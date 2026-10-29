"""iOS reverse CLI bridges (class-dump, etc.).

class-dump is macOS-only and not bundled; probe via ``reverse_toolchain_scan``.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from argusai.core.reverse.toolchain import detect_tool

_DEFAULT_CLASS_DUMP_TIMEOUT = 180


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def run_class_dump(
    binary_path: Path,
    output_dir: Path,
    *,
    timeout_seconds: int = _DEFAULT_CLASS_DUMP_TIMEOUT,
) -> dict[str, Any]:
    """Run ``class-dump -H`` against a Mach-O binary into ``output_dir``."""
    binary_path = binary_path.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not binary_path.is_file():
        return {"ok": False, "error": f"binary not found: {binary_path}"}

    info = detect_tool("class-dump")
    exe = str(info.get("path") or "").strip()
    if not info.get("available") or not exe:
        return {
            "ok": False,
            "blocked": True,
            "do_not_retry": True,
            "error": "class-dump not installed (macOS host tool; add to PATH or settings.toolchain.paths)",
            "toolchain": info,
        }

    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    cmd = [exe, "-H", "-o", str(output_dir), str(binary_path)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(30, min(int(timeout_seconds or _DEFAULT_CLASS_DUMP_TIMEOUT), 3600)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "partial": True,
            "error": f"class-dump timed out after {timeout_seconds}s",
            "output_dir": str(output_dir),
            "command": cmd,
        }
    except OSError as exc:
        return {"ok": False, "error": str(exc), "command": cmd}

    headers = sorted(output_dir.glob("*.h"))
    elapsed = round(time.time() - started, 2)
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()
    ok = proc.returncode == 0 or bool(headers)
    return {
        "ok": ok,
        "partial": proc.returncode != 0 and bool(headers),
        "usable_output": bool(headers),
        "output_dir": str(output_dir),
        "header_count": len(headers),
        "headers_preview": [h.name for h in headers[:40]],
        "returncode": proc.returncode,
        "stderr_preview": stderr[:2000],
        "stdout_preview": stdout[:2000],
        "command": cmd,
        "elapsed_seconds": elapsed,
        "finished_at": _now_iso(),
        "toolchain": info,
    }
