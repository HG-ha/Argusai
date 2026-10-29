"""External reverse-engineering toolchain detection.

Probes PATH and optional vendor locations without bundling third-party
runtimes. Results are cached per process and written into
``ReverseJob.toolchain_status`` by higher layers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argusai.core.infra.win_stdio import quiet_subprocess_kwargs


@dataclass(frozen=True)
class ToolProbe:
    name: str
    commands: tuple[str, ...]
    version_args: tuple[str, ...] = ("--version",)
    optional: bool = True
    # Appended to the "not found" error for tools that are not expected on
    # PATH (commercial installers, host-specific builds), so the caller gets
    # an actionable message instead of a bare "not found on PATH".
    not_found_hint: str = ""


TOOL_PROBES: tuple[ToolProbe, ...] = (
    ToolProbe("frida", ("frida", "frida.exe"), version_args=("--version",)),
    ToolProbe("frida-trace", ("frida-trace", "frida-trace.exe")),
    ToolProbe("frida-ls-devices", ("frida-ls-devices",)),
    ToolProbe("frida-ps", ("frida-ps", "frida-ps.exe")),
    ToolProbe("objection", ("objection",)),
    ToolProbe("jadx", ("jadx", "jadx.bat")),
    ToolProbe("apktool", ("apktool", "apktool.bat")),
    ToolProbe("zipalign", ("zipalign", "zipalign.exe"), version_args=()),
    ToolProbe("apksigner", ("apksigner", "apksigner.bat")),
    ToolProbe("jarsigner", ("jarsigner",), version_args=("-version",)),
    ToolProbe("aapt", ("aapt", "aapt2")),
    ToolProbe("adb", ("adb", "adb.exe")),
    ToolProbe("class-dump", ("class-dump",)),
    ToolProbe("idevice_id", ("idevice_id",)),
    ToolProbe("lldb", ("lldb", "lldb.exe")),
    ToolProbe("ghidra", ("analyzeHeadless", "analyzeHeadless.bat"), version_args=()),
    # Headless (text mode) binaries first: those are the ones usable for
    # automation. version_args is empty because IDA has no version flag and
    # would otherwise treat the argument as a file to open in the GUI.
    ToolProbe(
        "ida",
        (
            "idat64",
            "idat64.exe",
            "idat",
            "idat.exe",
            "ida64",
            "ida64.exe",
            "ida",
            "ida.exe",
        ),
        version_args=(),
        not_found_hint=(
            "IDA is commercial and normally not on PATH — set "
            "settings.toolchain.paths.ida to the headless binary "
            "(idat64/idat, idat64.exe/idat.exe on Windows)"
        ),
    ),
    # Not a reverse tool, but it shares the "optional local binary the user may
    # have to point us at" plumbing: assistant video attachments degrade to
    # keyframes through it (see core/assistant/video_frames.py).
    ToolProbe("ffmpeg", ("ffmpeg", "ffmpeg.exe"), version_args=("-version",)),
    ToolProbe("ffprobe", ("ffprobe", "ffprobe.exe"), version_args=("-version",)),
    ToolProbe("yara", ("yara", "yara.exe")),
    ToolProbe("capa", ("capa", "capa.exe")),
    ToolProbe("innoextract", ("innoextract", "innoextract.exe")),
    ToolProbe("strings", ("strings",)),
    ToolProbe("dumpbin", ("dumpbin",)),
)

_CACHE: dict[str, dict[str, Any]] = {}
_PATH_OVERRIDES: dict[str, str] = {}

# settings.toolchain.paths key → other keys to try when resolving a probe path
_OVERRIDE_FALLBACK_KEYS: dict[str, tuple[str, ...]] = {}


def set_path_overrides(overrides: dict[str, str] | None) -> None:
    """Apply user-configured executable paths (from settings.toolchain.paths)."""
    global _PATH_OVERRIDES
    cleaned: dict[str, str] = {}
    for key, value in (overrides or {}).items():
        name = str(key or "").strip().lower()
        path = str(value or "").strip()
        if name and path:
            cleaned[name] = path
    _PATH_OVERRIDES = cleaned
    clear_toolchain_cache()


def sync_path_overrides_from_settings(settings: Any) -> None:
    paths = getattr(settings, "toolchain_paths", None) or {}
    if isinstance(paths, dict):
        set_path_overrides(paths)
    else:
        set_path_overrides({})


def get_path_overrides() -> dict[str, str]:
    return dict(_PATH_OVERRIDES)


def _resolve_executable(candidates: tuple[str, ...]) -> str | None:
    for name in candidates:
        path = shutil.which(name)
        if path:
            return path
    return None


def _path_from_override(raw: str) -> str | None:
    override = str(raw or "").strip()
    if not override:
        return None
    path = Path(override).expanduser()
    if path.is_file():
        return str(path.resolve())
    return None


# Small reverse tools retained in packaged builds. Maps probe name to its
# vendor subdirectory; this does not include Python or Node runtimes.
_VENDORED_REVERSE_TOOLS: dict[str, str] = {"innoextract": "innoextract"}


def _vendor_tool_path(name: str) -> str | None:
    """Resolve a bundled (vendored) reverse tool binary, if present.

    Returns None cheaply on a fresh checkout / unsupported platform (the
    vendor subdir simply will not exist), so this is safe to call on every
    probe. The result is cached per process by ``detect_tool``.
    """
    subdir = _VENDORED_REVERSE_TOOLS.get(name)
    if not subdir:
        return None
    try:
        from argusai.core.reverse.runtime_manager import _platform_subdir, _vendor_dir
    except Exception:
        return None
    base = _vendor_dir() / subdir / _platform_subdir()
    if not base.is_dir():
        return None
    exe = f"{name}.exe" if sys.platform.startswith("win") else name
    for cand in (base / exe, base / "bin" / exe):
        if cand.is_file():
            return str(cand.resolve())
    # Some portable archives nest the executable one level deeper.
    for cand in base.rglob(exe):
        if cand.is_file():
            return str(cand.resolve())
    return None


def _resolve_probe_path(probe: ToolProbe) -> str | None:
    keys_to_try = (probe.name, *_OVERRIDE_FALLBACK_KEYS.get(probe.name, ()))
    seen: set[str] = set()
    for key in keys_to_try:
        if key in seen:
            continue
        seen.add(key)
        resolved = _path_from_override(_PATH_OVERRIDES.get(key, ""))
        if resolved:
            return resolved
    # Bundled (pinned, known-good) binary beats whatever is on PATH; an
    # explicit settings override above still wins over the bundle.
    vendored = _vendor_tool_path(probe.name)
    if vendored:
        return vendored
    return _resolve_executable(probe.commands)


def _run_version(path: str, args: tuple[str, ...]) -> tuple[str, str]:
    try:
        proc = subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
            env=os.environ,
            **quiet_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc)
    out = (proc.stdout or proc.stderr or "").strip()
    first = out.splitlines()[0] if out else ""
    return first[:200], ""


def detect_tool(probe: ToolProbe | str) -> dict[str, Any]:
    if isinstance(probe, str):
        match = next((p for p in TOOL_PROBES if p.name == probe), None)
        if match is None:
            return {"name": probe, "available": False, "error": "unknown tool"}
        probe = match

    cached = _CACHE.get(probe.name)
    if cached is not None:
        return dict(cached)

    path = _resolve_probe_path(probe)
    if not path:
        override = _PATH_OVERRIDES.get(probe.name, "").strip()
        err = "configured path not found" if override else "not found on PATH"
        if not override and probe.not_found_hint:
            err = f"{err} — {probe.not_found_hint}"
        result = {
            "name": probe.name,
            "available": False,
            "optional": probe.optional,
            "path": override,
            "version": "",
            "error": err,
        }
        _CACHE[probe.name] = result
        return dict(result)

    if probe.version_args:
        version, err = _run_version(path, probe.version_args)
    else:
        version, err = "", ""
    result = {
        "name": probe.name,
        "available": True,
        "optional": probe.optional,
        "path": path,
        "version": version,
        "error": err,
        "platform": sys.platform,
    }
    _CACHE[probe.name] = result
    return dict(result)


def scan_toolchain(*, names: list[str] | None = None) -> dict[str, Any]:
    probes = TOOL_PROBES
    if names:
        wanted = {n.strip().lower() for n in names if str(n).strip()}
        probes = tuple(p for p in TOOL_PROBES if p.name in wanted)
    tools = {p.name: detect_tool(p) for p in probes}
    available = [name for name, info in tools.items() if info.get("available")]
    missing = [name for name, info in tools.items() if not info.get("available")]
    return {
        "ok": True,
        "platform": sys.platform,
        "available": available,
        "missing": missing,
        "tools": tools,
    }


def clear_toolchain_cache() -> None:
    _CACHE.clear()


# Maps CLI probe name → Agent tool name for fallback suggestions when a
# required binary is missing. Keep in sync with native_reverse executors.
_CLI_TOOL_ALTERNATIVES: dict[str, tuple[tuple[str, str], ...]] = {
    "jadx": (("apktool", "android_apktool_decode"),),
    "apktool": (("jadx", "android_jadx_decompile"),),
}


def tool_unavailable_error(
    tool: str,
    *,
    info: dict[str, Any] | None = None,
    alternatives: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Structured failure when an external CLI is not installed/configured.

    Returned to the Agent with ``blocked`` / ``do_not_retry`` so the loop
    does not waste rounds re-invoking the same missing binary. Callers
    should check ``detect_tool`` *before* doing heavy work (sample
    resolution, output dir creation, subprocess spawn).
    """
    from argusai.core.reverse.toolchain_install import install_spec, managed_reverse_root

    probe = info if info is not None else detect_tool(tool)
    alts = list(alternatives or [])
    if not alts:
        for alt_cli, alt_tool in _CLI_TOOL_ALTERNATIVES.get(tool, ()):
            if detect_tool(alt_cli).get("available"):
                alts.append({"cli": alt_cli, "tool": alt_tool})
    spec = install_spec(tool)
    can_install = bool(spec and spec.method not in {"manual"})
    install_root = str(managed_reverse_root())
    if can_install:
        suggested_fix = (
            f"With user confirmation, install {tool} via reverse_toolchain_install(tool={tool!r}) "
            f"or Settings → Reverse toolchain → Install. "
            f"Managed installs persist under {install_root} and update settings.toolchain.paths.{tool}. "
            "Run reverse_toolchain_scan after install. "
            "Do not use run_shell_snippet / sandbox_pip_install for reverse CLIs."
        )
        next_step = "reverse_toolchain_install"
    else:
        docs = spec.docs_url if spec else "official documentation"
        suggested_fix = (
            f"Install {tool} manually on the host (see {docs}). "
            f"Then add it to PATH or set settings.toolchain.paths.{tool}. "
            "Run reverse_toolchain_scan(job_id=...) before calling CLI bridge tools. "
            "Do not use run_shell_snippet / sandbox tools to install reverse CLIs."
        )
        next_step = "reverse_toolchain_scan"
    if alts:
        next_step = alts[0]["tool"]
    elif can_install and next_step == "reverse_toolchain_scan":
        next_step = "reverse_toolchain_install"
    return {
        "ok": False,
        "blocked": True,
        "do_not_retry": True,
        "toolchain_required": tool,
        "can_install": can_install,
        "install_root": install_root,
        "docs_url": spec.docs_url if spec else "",
        "error": f"{tool} not installed or not on PATH/settings.toolchain.paths",
        "suggested_fix": suggested_fix,
        "toolchain": probe,
        "alternatives": alts,
        "next_step": next_step,
    }
