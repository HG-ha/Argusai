"""Contained, offline-first helpers for IoT firmware assessment.

The helpers in this module inspect files that already exist on disk. They do
not boot firmware, start emulators, extract archives, mount filesystems, or
write to a target device. External frameworks are represented by environment
preflight records and safe plans only.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import platform
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable

_FIRMWARE_TOOLS = {
    "binwalk": ("binwalk",),
    "emba": ("emba", "emba.sh"),
    "firmae": ("firmae", "run.sh"),
    "yara": ("yara",),
    "capa": ("capa",),
}

_TEXT_SUFFIXES = frozenset({
    ".conf",
    ".cfg",
    ".ini",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".txt",
    ".sh",
    ".service",
    ".socket",
    ".target",
    ".timer",
    ".path",
    ".rules",
    ".list",
    ".properties",
    ".pem",
    ".crt",
    ".key",
    ".pub",
    ".lua",
    ".js",
    ".html",
    ".htm",
})

_SKIP_DIRS = frozenset({
    ".git",
    ".svn",
    "__pycache__",
    "dev",
    "proc",
    "sys",
    "run",
    "tmp",
    "mnt",
    "media",
    "lost+found",
})

_SERVICE_ROOTS = (
    ("systemd", "etc/systemd/system"),
    ("systemd", "lib/systemd/system"),
    ("systemd", "usr/lib/systemd/system"),
    ("sysv", "etc/init.d"),
    ("openrc", "etc/runlevels"),
    ("xinetd", "etc/xinetd.d"),
)

_SERVICE_MARKERS = (
    ("dropbear", "ssh"),
    ("sshd", "ssh"),
    ("telnetd", "telnet"),
    ("httpd", "http"),
    ("lighttpd", "http"),
    ("nginx", "http"),
    ("boa", "http"),
    ("uhttpd", "http"),
    ("mosquitto", "mqtt"),
    ("dnsmasq", "dns/dhcp"),
    ("upnp", "upnp"),
    ("miniupnpd", "upnp"),
    ("snmpd", "snmp"),
    ("ftpd", "ftp"),
)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_firmware_path(
    workspace_root: str | Path,
    path: str | Path,
    *,
    must_exist: bool = True,
    kind: str = "any",
) -> Path:
    """Resolve ``path`` below ``workspace_root`` and reject symlink traversal."""
    root = Path(workspace_root).expanduser().resolve()
    raw = Path(path).expanduser()
    target = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if target != root and not _is_relative_to(target, root):
        raise ValueError(f"path escapes firmware workspace: {path}")
    if must_exist and not target.exists():
        raise FileNotFoundError(f"path not found: {target}")
    expected = str(kind or "any").strip().lower()
    if must_exist and expected == "file" and not target.is_file():
        raise ValueError(f"expected a file: {target}")
    if must_exist and expected in {"dir", "directory", "rootfs"} and not target.is_dir():
        raise ValueError(f"expected a directory: {target}")
    return target


resolve_contained_path = resolve_firmware_path


def firmware_path_status(
    workspace_root: str | Path,
    path: str | Path,
    *,
    must_exist: bool = True,
    kind: str = "any",
) -> dict[str, Any]:
    try:
        resolved = resolve_firmware_path(workspace_root, path, must_exist=must_exist, kind=kind)
    except (ValueError, FileNotFoundError, OSError) as exc:
        return {
            "ok": False,
            "blocked": True,
            "blocked_reason": "path_containment",
            "error": str(exc),
        }
    return {
        "ok": True,
        "blocked": False,
        "workspace_root": str(Path(workspace_root).expanduser().resolve()),
        "path": str(resolved),
        "kind": "directory" if resolved.is_dir() else "file" if resolved.is_file() else "missing",
    }


def _candidate_executable(
    names: Iterable[str],
    *,
    override: str = "",
    which: Callable[[str], str | None] = shutil.which,
) -> str:
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_dir():
            for name in names:
                nested = candidate / name
                if nested.is_file():
                    return str(nested.resolve())
        elif candidate.is_file():
            return str(candidate.resolve())
    for name in names:
        found = which(name)
        if found:
            return str(Path(found).resolve())
    return ""


def firmware_toolchain_preflight(
    *,
    paths: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    platform_name: str = "",
) -> dict[str, Any]:
    """Probe Binwalk/EMBA/FirmAE and ReverseJob-adjacent static analyzers."""
    overrides = {str(k).lower(): str(v) for k, v in (paths or {}).items()}
    tools: dict[str, dict[str, Any]] = {}
    for tool, names in _FIRMWARE_TOOLS.items():
        resolved = _candidate_executable(names, override=overrides.get(tool, ""), which=which)
        notes: list[str] = []
        if tool in {"emba", "firmae"}:
            notes.append("Linux-oriented framework; this preflight does not start analysis or emulation.")
        if tool == "firmae":
            notes.append("FirmAE requires a separately prepared Linux virtualization environment.")
        if tool == "capa":
            notes.append("Use capa on extracted PE/ELF executables, not an opaque raw firmware blob.")
        tools[tool] = {
            "available": bool(resolved),
            "path": resolved,
            "blocked": not bool(resolved),
            "blocked_reason": "" if resolved else "missing_tool",
            "notes": notes,
        }
    system = (platform_name or platform.system()).strip().lower()
    return {
        "ok": True,
        "platform": system,
        "mode": "preflight-only",
        "tools": tools,
        "available": [name for name, info in tools.items() if info["available"]],
        "missing": [name for name, info in tools.items() if not info["available"]],
        "constraints": [
            "No automatic host installation.",
            "No firmware boot or emulation is started by preflight.",
            "All firmware/rootfs paths must remain inside the declared workspace.",
            "YARA can inspect the image or extracted files; capa is for supported extracted executables.",
        ],
    }


firmware_environment_preflight = firmware_toolchain_preflight


def _iter_rootfs_files(
    root: Path,
    *,
    glob: str = "",
    max_files: int = 5000,
) -> Iterable[Path]:
    count = 0
    pattern = str(glob or "").strip()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _SKIP_DIRS
            and not (base / name).is_symlink()
            and _is_relative_to((base / name).resolve(), root)
        ]
        for filename in filenames:
            candidate = base / filename
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if candidate.is_symlink() or not resolved.is_file() or not _is_relative_to(resolved, root):
                continue
            rel = resolved.relative_to(root).as_posix()
            if pattern and not fnmatch.fnmatch(rel, pattern) and not fnmatch.fnmatch(filename, pattern):
                continue
            if not pattern and resolved.suffix.lower() not in _TEXT_SUFFIXES and filename not in {
                "passwd",
                "shadow",
                "group",
                "fstab",
                "inittab",
                "rc.local",
                "profile",
                "authorized_keys",
            }:
                continue
            yield resolved
            count += 1
            if count >= max(1, min(int(max_files or 5000), 20000)):
                return


def rootfs_grep(
    rootfs_dir: str | Path,
    pattern: str,
    *,
    glob: str = "",
    regex: bool = True,
    case_sensitive: bool = False,
    max_matches: int = 200,
    max_files: int = 5000,
    max_file_bytes: int = 2 * 1024 * 1024,
) -> dict[str, Any]:
    """Search bounded text files in an already-extracted rootfs."""
    root = Path(rootfs_dir).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "error": f"rootfs directory not found: {root}"}
    query = str(pattern or "")
    if not query:
        return {"ok": False, "error": "pattern is required"}
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        expression = re.compile(query if regex else re.escape(query), flags)
    except re.error as exc:
        return {"ok": False, "error": f"invalid regex: {exc}"}
    cap = max(1, min(int(max_matches or 200), 2000))
    byte_cap = max(1024, min(int(max_file_bytes or 2 * 1024 * 1024), 10 * 1024 * 1024))
    matches: list[dict[str, Any]] = []
    files_scanned = 0
    files_skipped = 0
    truncated = False
    for path in _iter_rootfs_files(root, glob=glob, max_files=max_files):
        try:
            if path.stat().st_size > byte_cap:
                files_skipped += 1
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            files_skipped += 1
            continue
        files_scanned += 1
        rel = path.relative_to(root).as_posix()
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not expression.search(line):
                continue
            matches.append({
                "path": rel,
                "line": line_number,
                "text": line[:1000],
            })
            if len(matches) >= cap:
                truncated = True
                break
        if truncated:
            break
    return {
        "ok": True,
        "mode": "offline-read-only",
        "rootfs": str(root),
        "pattern": query,
        "matches": matches,
        "match_count": len(matches),
        "files_scanned": files_scanned,
        "files_skipped": files_skipped,
        "truncated": truncated,
    }


grep_rootfs = rootfs_grep


def _parse_systemd_unit(path: Path, root: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    values: dict[str, list[str]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in {"ExecStart", "ExecStartPre", "User", "Group", "ListenStream", "ListenDatagram", "EnvironmentFile"}:
            values.setdefault(key, []).append(value.strip())
    return {
        "name": path.name,
        "manager": "systemd",
        "path": path.relative_to(root).as_posix(),
        "exec_start": values.get("ExecStart", []),
        "exec_start_pre": values.get("ExecStartPre", []),
        "user": (values.get("User") or [""])[0],
        "group": (values.get("Group") or [""])[0],
        "listen": [*values.get("ListenStream", []), *values.get("ListenDatagram", [])],
        "environment_files": values.get("EnvironmentFile", []),
    }


def _script_service(path: Path, root: Path, manager: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:200_000]
    except OSError:
        text = ""
    commands: list[str] = []
    protocols: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lower = stripped.lower()
        if any(marker in lower for marker, _protocol in _SERVICE_MARKERS):
            commands.append(stripped[:500])
        for marker, protocol in _SERVICE_MARKERS:
            if marker in lower and protocol not in protocols:
                protocols.append(protocol)
        if len(commands) >= 12:
            break
    return {
        "name": path.name,
        "manager": manager,
        "path": path.relative_to(root).as_posix(),
        "command_hints": commands,
        "protocol_hints": protocols,
    }


def map_rootfs_services(
    rootfs_dir: str | Path,
    *,
    max_services: int = 500,
) -> dict[str, Any]:
    """Map common init/service definitions from an extracted rootfs."""
    root = Path(rootfs_dir).expanduser().resolve()
    if not root.is_dir():
        return {"ok": False, "error": f"rootfs directory not found: {root}"}
    cap = max(1, min(int(max_services or 500), 2000))
    services: list[dict[str, Any]] = []
    seen: set[str] = set()
    for manager, relative in _SERVICE_ROOTS:
        service_root = (root / relative).resolve()
        if not _is_relative_to(service_root, root) or not service_root.is_dir():
            continue
        for path in sorted(service_root.rglob("*")):
            if len(services) >= cap:
                break
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if path.is_symlink() or not resolved.is_file() or not _is_relative_to(resolved, root):
                continue
            if manager == "systemd" and resolved.suffix not in {".service", ".socket", ".timer", ".path"}:
                continue
            rel = resolved.relative_to(root).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            services.append(
                _parse_systemd_unit(resolved, root)
                if manager == "systemd"
                else _script_service(resolved, root, manager)
            )
    for special in ("etc/inittab", "etc/rc.local"):
        path = (root / special).resolve()
        if len(services) >= cap or not path.is_file() or not _is_relative_to(path, root):
            continue
        services.append(_script_service(path, root, "busybox/init"))
    protocols = sorted({
        hint
        for service in services
        for hint in service.get("protocol_hints", [])
        if hint
    })
    return {
        "ok": True,
        "mode": "offline-read-only",
        "rootfs": str(root),
        "service_count": len(services),
        "services": services,
        "protocol_hints": protocols,
        "truncated": len(services) >= cap,
        "next_step": "Correlate service paths with rootfs_grep results and inspect extracted executables via ReverseJob.",
    }


rootfs_service_map = map_rootfs_services


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_firmware_analysis_plan(
    firmware_path: str | Path,
    *,
    workspace_root: str | Path,
    job_id: str = "",
    rules_path: str = "",
    paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a contained static workflow without running frameworks."""
    try:
        firmware = resolve_firmware_path(workspace_root, firmware_path, kind="file")
        rules = (
            resolve_firmware_path(workspace_root, rules_path, kind="file")
            if str(rules_path or "").strip()
            else None
        )
    except (ValueError, FileNotFoundError, OSError) as exc:
        return {
            "ok": False,
            "blocked": True,
            "blocked_reason": "path_containment",
            "error": str(exc),
        }
    preflight = firmware_toolchain_preflight(paths=paths)
    reverse_calls: list[dict[str, Any]] = []
    if job_id:
        reverse_calls.append({
            "tool": "binary_yara_scan",
            "arguments": {
                "job_id": job_id,
                "path": str(firmware),
                "rules_path": str(rules) if rules else "<authorized rules file>",
            },
            "condition": "yara available and a contained rules file is supplied",
        })
        reverse_calls.append({
            "tool": "binary_capa_analyze",
            "arguments": {"job_id": job_id, "path": "<contained extracted PE/ELF artifact>"},
            "condition": "capa available; never point capa at an unsupported opaque image",
        })
    return {
        "ok": True,
        "blocked": False,
        "mode": "offline-plan",
        "firmware": {
            "path": str(firmware),
            "relative_path": firmware.relative_to(Path(workspace_root).expanduser().resolve()).as_posix(),
            "size": firmware.stat().st_size,
            "sha256": _sha256_file(firmware),
        },
        "toolchain": preflight,
        "steps": [
            {
                "id": "identify",
                "tool": "binwalk",
                "mode": "read-only signature/entropy listing",
                "blocked": not preflight["tools"]["binwalk"]["available"],
            },
            {
                "id": "extract-outside-this-module",
                "action": "Use an approved extractor into a new contained workspace directory; never trust archive paths.",
            },
            {
                "id": "rootfs",
                "tools": ["rootfs_service_map", "rootfs_grep"],
                "action": "Map services and search bounded text/configuration files offline.",
            },
            {
                "id": "binary-triage",
                "tools": ["binary_yara_scan", "binary_capa_analyze"],
                "action": "Attach image/extracted executables to a ReverseJob and preserve artifact refs.",
            },
            {
                "id": "framework-review",
                "tools": ["EMBA", "FirmAE"],
                "action": "Use only after environment preflight and explicit lab authorization; this plan does not execute or emulate.",
            },
        ],
        "reverse_job": {
            "job_id": str(job_id or ""),
            "target_type": "native",
            "platform": "linux",
            "artifact_kind": "other",
            "note": "Record the firmware identity as an artifact; attach extracted PE/ELF files separately for capa.",
            "recommended_calls": reverse_calls,
        },
        "case_evidence": {
            "artifact_kind": "firmware",
            "evidence_kind": "firmware",
            "record": ["sha256", "size", "tool versions", "contained paths", "service map", "YARA/capa artifact refs"],
        },
    }


firmware_analysis_plan = build_firmware_analysis_plan


__all__ = [
    "build_firmware_analysis_plan",
    "firmware_analysis_plan",
    "firmware_environment_preflight",
    "firmware_path_status",
    "firmware_toolchain_preflight",
    "grep_rootfs",
    "map_rootfs_services",
    "resolve_contained_path",
    "resolve_firmware_path",
    "rootfs_grep",
    "rootfs_service_map",
]
