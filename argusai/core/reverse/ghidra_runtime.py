"""Ghidra headless backend for native function discovery, disassembly and decompile.

The backend drives NSA Ghidra through ``analyzeHeadless`` plus small Jython
post-scripts that write structured JSON to temporary files. An analyzed project
is cached per sample so multiple operations reuse the same program database.

Resolution order for the engine:
1. ``settings.toolchain.paths.ghidra`` → path to ``analyzeHeadless`` (or its dir)
2. ``GHIDRA_INSTALL_DIR`` env → ``<dir>/support/analyzeHeadless[.bat]``
3. ``analyzeHeadless`` on PATH

Requires a JDK on PATH (modern Ghidra needs JDK 21); a missing/incompatible JDK
surfaces as a clear error from the headless run.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from argusai.core.reverse.android_toolchain import _now_iso, _run_cli

_SCRIPTS_DIR = Path(__file__).parent / "ghidra_scripts"
_DECOMPILE_SCRIPT = "decompile_function.py"
_LIST_FUNCTIONS_SCRIPT = "list_functions.py"
_DISASSEMBLE_SCRIPT = "disassemble_function.py"
_PROJECT_LOCKS: dict[str, threading.Lock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()


def _headless_name() -> str:
    return "analyzeHeadless.bat" if sys.platform == "win32" else "analyzeHeadless"


def resolve_analyze_headless(api: Any | None = None) -> tuple[str | None, dict[str, Any]]:
    """Locate ``analyzeHeadless``; returns ``(path|None, info)``."""
    from argusai.core.reverse.toolchain import detect_tool

    # 1/3: settings path or PATH via the standard probe.
    info = detect_tool("ghidra")
    path = str(info.get("path") or "").strip()
    if info.get("available") and path:
        p = Path(path)
        # A configured path may point at the install dir rather than the script.
        if p.is_dir():
            cand = p / "support" / _headless_name()
            if cand.is_file():
                return str(cand.resolve()), info
        elif p.is_file():
            return str(p.resolve()), info

    # 2/3: GHIDRA_INSTALL_DIR env convention.
    install_dir = os.environ.get("GHIDRA_INSTALL_DIR", "").strip()
    if install_dir:
        cand = Path(install_dir) / "support" / _headless_name()
        if cand.is_file():
            return str(cand.resolve()), {"name": "ghidra", "available": True, "path": str(cand.resolve()), "source": "GHIDRA_INSTALL_DIR"}

    return None, info


def ghidra_available(api: Any | None = None) -> bool:
    path, _info = resolve_analyze_headless(api)
    return path is not None


def _project_dir(api: Any | None) -> Path:
    from argusai.core.reverse.toolchain_install import managed_reverse_root

    data_root = None
    try:
        mgr = getattr(api, "settings_mgr", None)
        if mgr is not None:
            data_root = getattr(mgr.get(), "data_root", None)
    except Exception:
        data_root = None
    root = managed_reverse_root(data_root) / "ghidra_projects"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _project_key(sample: Path) -> str:
    h = hashlib.sha256()
    try:
        with sample.open("rb") as fh:
            h.update(fh.read(1024 * 1024))  # first 1MB is enough to key the cache
        h.update(str(sample.stat().st_size).encode())
    except OSError:
        h.update(str(sample.resolve()).encode())
    return h.hexdigest()[:12]


def _lock_for(key: str) -> threading.Lock:
    with _PROJECT_LOCKS_GUARD:
        lock = _PROJECT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROJECT_LOCKS[key] = lock
        return lock


def _build_argv(
    headless: str,
    proj_dir: Path,
    proj_name: str,
    sample: Path,
    *,
    imported: bool,
    script_name: str,
    script_args: list[str],
    out_file: Path,
) -> list[str]:
    argv: list[str] = [headless, str(proj_dir), proj_name]
    if imported:
        argv += ["-process", sample.name, "-noanalysis"]
    else:
        argv += ["-import", str(sample.resolve())]
    argv += [
        "-scriptPath", str(_SCRIPTS_DIR.resolve()),
        "-postScript", script_name, *script_args, str(out_file.resolve()),
    ]
    # Windows .bat must be launched through cmd to avoid WinError 193.
    if sys.platform == "win32" and headless.lower().endswith(".bat"):
        return ["cmd", "/c", *argv]
    return argv


def _run_json_script(
    api: Any | None,
    sample: Path,
    *,
    script_name: str,
    script_args: list[str],
    timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    """Run one JSON-producing Ghidra post-script against a cached project."""
    headless, info = resolve_analyze_headless(api)
    if headless is None:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return {}, {}, info, tool_unavailable_error("ghidra", info=info)
    if not sample.is_file():
        return {}, {}, info, {"ok": False, "error": f"sample not found: {sample}"}

    key = _project_key(sample)
    proj_dir = _project_dir(api)
    proj_name = f"argus_{key}"
    gpr = proj_dir / f"{proj_name}.gpr"

    out_fd, out_name = tempfile.mkstemp(suffix=".ghidra.json")
    os.close(out_fd)
    out_file = Path(out_name)
    out_file.unlink(missing_ok=True)

    lock = _lock_for(key)
    with lock:
        imported = gpr.is_file()
        argv = _build_argv(
            headless,
            proj_dir,
            proj_name,
            sample,
            imported=imported,
            script_name=script_name,
            script_args=script_args,
            out_file=out_file,
        )
        run = _run_cli(argv, timeout_seconds=max(60, int(timeout_seconds or 600)))
        if imported and not out_file.is_file():
            argv2 = _build_argv(
                headless,
                proj_dir,
                f"{proj_name}_r",
                sample,
                imported=False,
                script_name=script_name,
                script_args=script_args,
                out_file=out_file,
            )
            run = _run_cli(argv2, timeout_seconds=max(60, int(timeout_seconds or 600)))

    payload: dict[str, Any] = {}
    if out_file.is_file():
        try:
            payload = json.loads(out_file.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            payload = {}
        finally:
            out_file.unlink(missing_ok=True)
    return payload, run, info, None


def decompile_function(
    api: Any | None,
    sample: Path,
    *,
    function: str = "",
    address: str = "",
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Decompile one function with Ghidra headless."""
    target = str(address or "").strip() or str(function or "").strip() or "entry"
    started = _now_iso()
    payload, run, info, early_error = _run_json_script(
        api,
        sample,
        script_name=_DECOMPILE_SCRIPT,
        script_args=[target],
        timeout_seconds=timeout_seconds,
    )
    if early_error is not None:
        return early_error

    if payload.get("ok"):
        pseudo = str(payload.get("c") or "")
        return {
            "ok": True,
            "usable_output": bool(pseudo),
            "tool": "ghidra",
            "engine": "ghidra",
            "engine_note": "Ghidra decompiler — IDA/Hex-Rays-class pseudocode",
            "input": str(sample.resolve()),
            "target": target,
            "function": payload.get("function") or "",
            "entry": payload.get("entry") or "",
            "toolchain": info,
            "started_at": started,
            "finished_at": _now_iso(),
            "pseudocode": pseudo[:16000],
            "pseudocode_truncated": len(pseudo) > 16000,
            "next_step": "Cross-check with binary_disassemble; use binary_patch to modify the function, then re-run to verify.",
        }
    err = payload.get("error") or run.get("error") or run.get("stderr") or "ghidra decompile produced no output"
    return {
        "ok": False,
        "tool": "ghidra",
        "engine": "ghidra",
        "input": str(sample.resolve()),
        "target": target,
        "toolchain": info,
        "started_at": started,
        "finished_at": _now_iso(),
        "error": str(err)[:1000],
        "stderr_preview": str(run.get("stderr") or "")[:1500],
    }


def list_functions(
    api: Any | None,
    sample: Path,
    *,
    limit: int = 400,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """List analyzed functions using Ghidra's function manager."""
    cap = max(1, min(int(limit or 400), 5000))
    started = _now_iso()
    payload, run, info, early_error = _run_json_script(
        api,
        sample,
        script_name=_LIST_FUNCTIONS_SCRIPT,
        script_args=[str(cap)],
        timeout_seconds=timeout_seconds,
    )
    if early_error is not None:
        return early_error

    raw_functions = payload.get("functions") if isinstance(payload.get("functions"), list) else []
    functions = [item for item in raw_functions if isinstance(item, dict)]
    function_count = int(payload.get("function_count") or len(functions))
    ok = bool(payload.get("ok")) and bool(functions)
    return {
        "ok": ok,
        "usable_output": bool(functions),
        "tool": "ghidra",
        "engine": "ghidra",
        "input": str(sample.resolve()),
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
        "function_count": function_count,
        "functions": functions[:cap],
        "truncated": bool(payload.get("truncated")) or function_count > cap,
        "error": "" if ok else str(
            payload.get("error")
            or run.get("error")
            or run.get("stderr")
            or "Ghidra found no functions (binary may be packed or stripped)"
        )[:1000],
        "next_step": "Pick a function name or entry address, then call binary_disassemble or binary_decompile.",
    }


def disassemble_function(
    api: Any | None,
    sample: Path,
    *,
    function: str = "",
    address: str = "",
    count: int = 0,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Disassemble a function or bounded instruction range with Ghidra."""
    target = str(address or "").strip() or str(function or "").strip() or "entry"
    instruction_limit = max(0, min(int(count or 0), 4000))
    started = _now_iso()
    payload, run, info, early_error = _run_json_script(
        api,
        sample,
        script_name=_DISASSEMBLE_SCRIPT,
        script_args=[target, str(instruction_limit)],
        timeout_seconds=timeout_seconds,
    )
    if early_error is not None:
        return early_error

    disasm = str(payload.get("disasm") or "").strip()
    ok = bool(payload.get("ok")) and bool(disasm)
    return {
        "ok": ok,
        "usable_output": bool(disasm),
        "tool": "ghidra",
        "engine": "ghidra",
        "input": str(sample.resolve()),
        "target": target,
        "function": str(payload.get("function") or ""),
        "entry": str(payload.get("entry") or ""),
        "mode": str(payload.get("mode") or ("function" if instruction_limit == 0 else f"{instruction_limit} instructions")),
        "instruction_count": int(payload.get("instruction_count") or 0),
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
        "disasm": disasm[:16000],
        "disasm_truncated": len(disasm) > 16000 or bool(payload.get("truncated")),
        "error": "" if ok else str(
            payload.get("error")
            or run.get("error")
            or run.get("stderr")
            or "Ghidra produced no disassembly for the requested target"
        )[:1000],
        "next_step": "Use binary_decompile on the same target for pseudocode, or binary_patch to modify bytes.",
    }
