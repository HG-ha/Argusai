"""External JS / WASM toolchain runtime manager.

Resolves the on-demand managed Node.js / webcrack / restringer stack under
``data_root/tools/runtimes/node`` plus the separately bundled WABT tools.
Normal application tools never silently fall back to a system Node binary.

Public surface:

- :func:`vendor_status` — health dict, drives the UI's "Runtime" page.
- :func:`node_bin` — absolute path to a usable ``node`` (raises if none).
- :func:`run_node_script` — synchronous subprocess wrapper with proper
  stdin/stdout/timeout/utf-8/error handling.
- :func:`webcrack_run` — wraps ``node webcrack_run.mjs`` with an input
  bundle, returns ``{ok, code, bundle, markers, error}``.
- :func:`restringer_run` — wraps ``node restringer_run.mjs``.
- :func:`wabt_run` — wraps ``wasm-decompile`` etc. (used by P5).

The actual ``*.mjs`` runner scripts live in
``argusai/vendor/runners/`` and are tiny wrappers (~30 LOC each) that
import the respective npm package, run it on stdin, and emit JSON on
stdout. Keeping them in-tree means we don't depend on a CLI shape we
can't pin (which has bitten webcrack users on upgrades).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argusai.core.infra.win_stdio import quiet_subprocess_kwargs
from argusai.core.managed_runtimes import (
    NODE_VERSION,
    node_executable,
    node_tools_dir,
    runtime_root as managed_runtime_root,
    runtime_status as managed_runtime_status,
    vendor_assets_dir,
)
from argusai.core.paths import PathManager

log = logging.getLogger("argusai.runtime_manager")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Return the directory that contains the ``argusai/`` package.

    Works both in dev (running from source) and in a Nuitka one-file
    build (in which case the runtime tree is extracted next to the exe).
    """
    here = Path(__file__).resolve()
    # argusai/core/reverse/runtime_manager.py → up four parents = repo root
    # (parent → reverse/, parent.parent → core/, parent.parent.parent →
    # argusai/, one more → the dir that contains the argusai/ package).
    return here.parent.parent.parent.parent


def _vendor_dir() -> Path:
    """``argusai/vendor/`` — retained WABT/innoextract/runner asset root."""
    # Prefer the shared resolver (handles macOS .app Contents/Resources).
    bundled = vendor_assets_dir()
    if bundled.is_dir():
        return bundled
    return _repo_root() / "argusai" / "vendor"


def _platform_subdir() -> str:
    """Subdirectory name for the current OS/arch, e.g. ``win-x64``.

    Matches the naming convention used by retained vendor tool archives.
    """
    sys_name = platform.system().lower()
    machine = platform.machine().lower()
    if sys_name.startswith("win"):
        os_label = "win"
    elif sys_name == "darwin":
        os_label = "macos"
    else:
        os_label = "linux"
    if machine in {"x86_64", "amd64"}:
        arch_label = "x64"
    elif machine in {"arm64", "aarch64"}:
        arch_label = "arm64"
    elif machine.startswith("i"):
        arch_label = "x86"
    else:
        arch_label = machine
    return f"{os_label}-{arch_label}"


def _node_binary_name() -> str:
    return "node.exe" if sys.platform.startswith("win") else "node"


def _which(cmd: str) -> str | None:
    """``shutil.which`` wrapper that returns ``None`` rather than empty."""
    p = shutil.which(cmd)
    return p if p else None


def _tools_dir(api: Any | None) -> Path:
    """Resolve the active ``data_root/tools`` without depending on cwd."""
    if api is None:
        return PathManager().tools_dir
    try:
        settings_mgr = getattr(api, "settings_mgr", None)
        if settings_mgr is not None:
            return PathManager(data_root=settings_mgr.get().data_root).tools_dir
        cfg = api.get_settings() or {}
        return PathManager(data_root=str(cfg.get("data_root") or "") or None).tools_dir
    except Exception:
        return PathManager().tools_dir


# ---------------------------------------------------------------------------
# Public status surface
# ---------------------------------------------------------------------------


@dataclass
class ToolPath:
    name: str
    found: bool
    source: str  # "managed" | "vendor" | "system" | "missing"
    path: str = ""
    version: str = ""
    error: str = ""


@dataclass
class RuntimeStatus:
    """Snapshot the UI / API renders as the "Runtime" page."""
    platform: str = ""
    vendor_dir: str = ""
    node: ToolPath = field(default_factory=lambda: ToolPath("node", False, "missing"))
    webcrack: ToolPath = field(default_factory=lambda: ToolPath("webcrack", False, "missing"))
    restringer: ToolPath = field(default_factory=lambda: ToolPath("restringer", False, "missing"))
    wabt: ToolPath = field(default_factory=lambda: ToolPath("wasm-decompile", False, "missing"))
    install_script: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "vendor_dir": self.vendor_dir,
            "node": self.node.__dict__,
            "webcrack": self.webcrack.__dict__,
            "restringer": self.restringer.__dict__,
            "wabt": self.wabt.__dict__,
            "install_script": self.install_script,
        }


def _resolve_node(api: Any | None) -> ToolPath:
    status = managed_runtime_status("node", _tools_dir(api))
    path = str(status.get("executable") or "")
    if status.get("healthy") and path:
        return ToolPath(
            name="node",
            found=True,
            source="managed",
            path=path,
            version=str(status.get("version") or ""),
        )
    return ToolPath(
        name="node",
        found=False,
        source="missing",
        path=path,
        version=str(status.get("version") or ""),
        error=str(status.get("error") or "managed Node sandbox environment is not installed"),
    )


def _probe_node(path: Path, *, source: str) -> ToolPath:
    """Run ``node -v`` to confirm a candidate Node binary works and report its version."""
    try:
        r = subprocess.run(
            [str(path), "-v"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=5,
            **quiet_subprocess_kwargs(),
        )
        ver = r.stdout.decode("utf-8", errors="replace").strip()
        if r.returncode != 0:
            return ToolPath(name="node", found=False, source=source, path=str(path),
                            error=f"node -v exited {r.returncode}")
        # Make sure it's the pinned Node line required by the managed stack.
        major = 0
        if ver.startswith("v"):
            try:
                major = int(ver[1:].split(".", 1)[0])
            except ValueError:
                major = 0
        if major != int(NODE_VERSION.split(".", 1)[0]):
            return ToolPath(name="node", found=False, source=source, path=str(path),
                            version=ver,
                            error=f"Node {ver} does not match managed Node {NODE_VERSION}.")
        return ToolPath(name="node", found=True, source=source, path=str(path), version=ver)
    except subprocess.TimeoutExpired:
        return ToolPath(name="node", found=False, source=source, path=str(path),
                        error="node -v timed out")
    except FileNotFoundError:
        return ToolPath(name="node", found=False, source=source, path=str(path),
                        error="node binary not executable")
    except Exception as e:
        return ToolPath(name="node", found=False, source=source, path=str(path),
                        error=f"{type(e).__name__}: {e}")


def _resolve_webcrack(api: Any | None) -> ToolPath:
    """Look for webcrack in the managed Node lockfile environment.

    We check for the package's main entry ``node_modules/webcrack/dist/index.js``
    rather than the bin script because we always invoke it via our wrapper
    (``runners/webcrack_run.mjs``), which imports the package directly.
    """
    base = node_tools_dir(_tools_dir(api))
    candidate = base / "node_modules" / "webcrack" / "dist" / "index.js"
    pkg_json = base / "node_modules" / "webcrack" / "package.json"
    if candidate.exists() and pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            ver = str(pkg.get("version") or "")
        except Exception:
            ver = ""
        return ToolPath(name="webcrack", found=True, source="managed",
                        path=str(base), version=ver)
    return ToolPath(name="webcrack", found=False, source="missing",
                    error="webcrack is missing from the managed Node sandbox environment.")


def _resolve_restringer(api: Any | None) -> ToolPath:
    base = node_tools_dir(_tools_dir(api))
    candidate = base / "node_modules" / "restringer" / "src" / "restringer.js"
    pkg_json = base / "node_modules" / "restringer" / "package.json"
    if candidate.exists() and pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            ver = str(pkg.get("version") or "")
        except Exception:
            ver = ""
        return ToolPath(name="restringer", found=True, source="managed",
                        path=str(base), version=ver)
    return ToolPath(name="restringer", found=False, source="missing",
                    error="restringer is missing from the managed Node sandbox environment.")


def _resolve_wabt(api: Any | None = None) -> ToolPath:
    """``wasm-decompile`` (and friends) from the WABT release.

    P5 uses this for WASM disassembly. We only check for ``wasm-decompile``
    here; the full set is auto-discovered relative to it in ``wabt_run``.

    Packaged builds carry no WABT, so the managed install comes first. A
    source checkout may still have ``argusai/vendor/wabt`` from an older
    ``vendor_runtime.py`` run, and a developer may have WABT on PATH.
    """
    status = managed_runtime_status("wabt", _tools_dir(api))
    if status.get("healthy") and status.get("executable"):
        return _probe_wabt(Path(str(status["executable"])))

    bin_name = "wasm-decompile.exe" if sys.platform.startswith("win") else "wasm-decompile"
    cand = _vendor_dir() / "wabt" / _platform_subdir() / "bin" / bin_name
    if cand.exists():
        return _probe_wabt(cand)
    sys_w = _which("wasm-decompile")
    if sys_w:
        return _probe_wabt(Path(sys_w))
    return ToolPath(
        name="wasm-decompile",
        found=False,
        source="missing",
        error=(
            "WABT is not installed. Install it from Settings → Sandbox runtimes, "
            "or run the runtime_install tool with runtime='wabt'."
        ),
    )


def _probe_wabt(path: Path) -> ToolPath:
    text = str(path)
    if str(managed_runtime_root("wabt")) in text:
        source = "managed"
    elif text.startswith(str(_vendor_dir())):
        source = "vendor"
    else:
        source = "system"
    try:
        r = subprocess.run(
            [str(path), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
            **quiet_subprocess_kwargs(),
        )
        ver = (r.stdout or b"").decode("utf-8", errors="replace").strip() or "?"
        if r.returncode != 0:
            return ToolPath(name="wasm-decompile", found=False, source=source,
                            path=str(path), error=f"--version exited {r.returncode}")
        return ToolPath(name="wasm-decompile", found=True, source=source,
                        path=str(path), version=ver)
    except Exception as e:
        return ToolPath(name="wasm-decompile", found=False, source=source,
                        path=str(path), error=f"{type(e).__name__}: {e}")


_STATUS_CACHE: tuple[float, str, RuntimeStatus] | None = None
_CACHE_TTL_SEC = 30.0


def vendor_status(api: Any | None = None, *, force: bool = False) -> RuntimeStatus:
    """Health snapshot. Cached briefly to keep UI polls cheap.

    The UI calls this from the Settings → Runtime page every time it's
    opened; without the cache that means a subprocess invocation per
    nav (~50 ms hit). Cache invalidates on `force=True` (the "Refresh"
    button) and after `_CACHE_TTL_SEC`.
    """
    global _STATUS_CACHE
    import time
    now = time.time()
    tools_dir = _tools_dir(api)
    cache_key = str(tools_dir)
    if (
        not force
        and _STATUS_CACHE is not None
        and _STATUS_CACHE[1] == cache_key
        and now - _STATUS_CACHE[0] < _CACHE_TTL_SEC
    ):
        return _STATUS_CACHE[2]

    status = RuntimeStatus(
        platform=_platform_subdir(),
        vendor_dir=str(_vendor_dir()),
        node=_resolve_node(api),
        webcrack=_resolve_webcrack(api),
        restringer=_resolve_restringer(api),
        wabt=_resolve_wabt(api),
        install_script="Settings → Sandbox runtimes",
    )
    _STATUS_CACHE = (now, cache_key, status)
    return status


def node_bin(api: Any | None = None) -> str:
    """Absolute path to a usable Node.js binary, or raise."""
    s = vendor_status(api)
    if not s.node.found:
        raise RuntimeError(
            f"Node.js runtime not available: {s.node.error}. "
            "Install the Node sandbox environment from the confirmation card or Settings."
        )
    return s.node.path


# ---------------------------------------------------------------------------
# Subprocess wrappers
# ---------------------------------------------------------------------------


def _runners_dir() -> Path:
    return _vendor_dir() / "runners"


def run_node_script(
    script: str | Path,
    *,
    input_text: str = "",
    argv: list[str] | None = None,
    cwd: Path | None = None,
    timeout: int = 60,
    api: Any | None = None,
    env_extra: dict[str, str] | None = None,
    node_modules_dir: Path | None = None,
) -> dict[str, Any]:
    """Run a Node script as a subprocess. Returns ``{ok, stdout, stderr,
    returncode, elapsed_ms, error}``.

    ``input_text`` is sent on stdin (utf-8). ``argv`` is the script's
    argv (we already pass the script path as argv[0]). ``cwd`` controls
    the working directory.

    ``node_modules_dir`` is the directory containing ``node_modules/`` that
    the script wants to ``import`` from. We set ``NODE_PATH`` to that
    location so the runners can reliably resolve packages even when
    invoked from far outside the install tree.
    """
    if argv is None:
        argv = []

    node = node_bin(api)
    cmd = [node, str(script)] + [str(a) for a in argv]

    if cwd is None:
        cwd = _tools_dir(api) / "runtimes" / "node" / ".work"
    env = node_process_env(cwd)
    for key in ("NODE_OPTIONS", "NODE_REPL_HISTORY"):
        env.pop(key, None)
    if env_extra:
        env.update(env_extra)
    if node_modules_dir is not None:
        existing = env.get("NODE_PATH", "")
        np = str((node_modules_dir / "node_modules").resolve())
        env["NODE_PATH"] = f"{np}{os.pathsep}{existing}" if existing else np

    import time
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            input=input_text.encode("utf-8") if input_text else b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            timeout=timeout,
            **quiet_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"timeout after {timeout}s",
            "returncode": -1,
            "stdout": "",
            "stderr": "",
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except FileNotFoundError as e:
        return {
            "ok": False,
            "error": f"node executable not found: {e}",
            "returncode": -1,
            "stdout": "",
            "stderr": "",
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "returncode": -1,
            "stdout": "",
            "stderr": "",
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    elapsed = int((time.time() - t0) * 1000)
    stdout = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
    stderr = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed_ms": elapsed,
        "error": "" if proc.returncode == 0 else f"exit {proc.returncode}",
    }


def node_process_env(cwd: Path) -> dict[str, str]:
    """Build a Node environment isolated to one sandbox working directory."""
    root = Path(cwd).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    session_tmp = root / ".tmp" / "node"
    session_cache = root / ".cache" / "node"
    session_tmp.mkdir(parents=True, exist_ok=True)
    session_cache.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(root)
    env["USERPROFILE"] = str(root)
    env["TMP"] = str(session_tmp)
    env["TEMP"] = str(session_tmp)
    env["npm_config_cache"] = str(session_cache)
    return env


# ---------------------------------------------------------------------------
# Tool wrappers — these are what tools in ``_tools.py`` actually call.
# Each wrapper:
#   1. Verifies the underlying tool is available.
#   2. Locates the runner script under vendor/runners/.
#   3. Sets NODE_PATH so the script can resolve its dependencies even
#      from outside its install tree.
#   4. Returns a clean dict — never raises on tool failures.
# ---------------------------------------------------------------------------


def webcrack_run(
    source: str,
    *,
    api: Any | None = None,
    timeout: int = 120,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run webcrack against ``source`` and return its result.

    Returns:
        ``{ok: bool, code: str, bundle: dict | None, markers: list,
           elapsed_ms: int, error: str}``

    ``options`` is forwarded to ``webcrack()``; sensible defaults are
    applied by the runner if you pass ``None``.
    """
    status = vendor_status(api)
    if not status.webcrack.found:
        return {
            "ok": False,
            "error": (
                f"webcrack not installed: {status.webcrack.error}. "
                "Install/repair the Node sandbox environment from Settings."
            ),
            "code": "",
            "bundle": None,
            "markers": [],
            "elapsed_ms": 0,
        }

    runner = _runners_dir() / "webcrack_run.mjs"
    if not runner.exists():
        return {
            "ok": False,
            "error": f"webcrack runner missing at {runner}. Reinstall the vendor toolchain.",
            "code": "",
            "bundle": None,
            "markers": [],
            "elapsed_ms": 0,
        }

    install_dir = Path(status.webcrack.path)
    payload = json.dumps({
        "source": source,
        "options": options or {},
        "webcrack_root": str(install_dir),
    }, ensure_ascii=False)

    result = run_node_script(
        runner,
        input_text=payload,
        timeout=timeout,
        api=api,
        node_modules_dir=install_dir,
    )
    if not result["ok"]:
        return {
            "ok": False,
            "error": result.get("error") or (result.get("stderr") or "").strip()[:500],
            "code": "",
            "bundle": None,
            "markers": [],
            "elapsed_ms": result.get("elapsed_ms") or 0,
            "stderr": (result.get("stderr") or "").strip()[:2000],
        }

    raw = result["stdout"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Runner crashed before emitting JSON.
        return {
            "ok": False,
            "error": "webcrack runner did not emit valid JSON",
            "code": "",
            "bundle": None,
            "markers": [],
            "elapsed_ms": result["elapsed_ms"],
            "stdout_preview": raw[:1000],
            "stderr": (result.get("stderr") or "").strip()[:2000],
        }

    return {
        "ok": bool(data.get("ok")),
        "code": str(data.get("code") or ""),
        "bundle": data.get("bundle"),
        "markers": list(data.get("markers") or []),
        "elapsed_ms": result["elapsed_ms"],
        "error": str(data.get("error") or ""),
        "deobfuscated": bool(data.get("deobfuscated")),
        "unminified": bool(data.get("unminified")),
        "unpacked": bool(data.get("unpacked")),
    }


def restringer_run(
    source: str,
    *,
    api: Any | None = None,
    timeout: int = 60,
    max_iterations: int = 6,
) -> dict[str, Any]:
    """Run restringer against ``source``. Restringer applies aggressive
    string-array / control-flow / dead-code reverse passes; it's slower
    than webcrack but sometimes catches obfuscator.io variants that
    webcrack's safer passes miss.

    Returns ``{ok, code, elapsed_ms, error}``.
    """
    status = vendor_status(api)
    if not status.restringer.found:
        return {
            "ok": False,
            "error": (
                f"restringer not installed: {status.restringer.error}. "
                "Install/repair the Node sandbox environment from Settings."
            ),
            "code": "",
            "elapsed_ms": 0,
        }
    runner = _runners_dir() / "restringer_run.mjs"
    if not runner.exists():
        return {
            "ok": False,
            "error": f"restringer runner missing at {runner}.",
            "code": "",
            "elapsed_ms": 0,
        }
    payload = json.dumps({"source": source, "maxIterations": int(max_iterations)})
    install_dir = Path(status.restringer.path)
    result = run_node_script(
        runner, input_text=payload, timeout=timeout,
        api=api, node_modules_dir=install_dir,
    )
    if not result["ok"]:
        return {
            "ok": False,
            "error": result.get("error") or (result.get("stderr") or "").strip()[:500],
            "code": "",
            "elapsed_ms": result.get("elapsed_ms") or 0,
            "stderr": (result.get("stderr") or "").strip()[:2000],
        }
    try:
        data = json.loads(result["stdout"])
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": "restringer runner did not emit valid JSON",
            "code": "",
            "elapsed_ms": result["elapsed_ms"],
            "stdout_preview": result["stdout"][:1000],
            "stderr": (result.get("stderr") or "").strip()[:2000],
        }
    return {
        "ok": bool(data.get("ok")),
        "code": str(data.get("code") or ""),
        "elapsed_ms": result["elapsed_ms"],
        "error": str(data.get("error") or ""),
        "iterations": int(data.get("iterations") or 0),
    }


def wabt_run(
    tool: str,
    *,
    args: list[str],
    cwd: Path | None = None,
    timeout: int = 30,
    api: Any | None = None,
) -> dict[str, Any]:
    """Run a single WABT executable (``wasm-decompile``, ``wasm2wat``,
    ``wasm-objdump``, ``wasm-strip``, ``wat2wasm``) with the given argv.

    P5 uses this. The tool name is resolved relative to the same bin
    directory the cached ``wasm-decompile`` lives in, so once one tool
    is found all the others are too.

    Returns ``{ok, stdout, stderr, returncode, elapsed_ms, error}``.
    """
    status = vendor_status(api)
    if not status.wabt.found:
        return {
            "ok": False,
            "error": str(status.wabt.error) or "WABT is not installed.",
            "runtime_required": "wabt",
            "stdout": "",
            "stderr": "",
            "returncode": -1,
            "elapsed_ms": 0,
        }
    base_dir = Path(status.wabt.path).parent
    suffix = ".exe" if sys.platform.startswith("win") else ""
    tool_path = base_dir / f"{tool}{suffix}"
    if not tool_path.exists():
        return {
            "ok": False,
            "error": f"WABT tool not found: {tool_path}",
            "stdout": "",
            "stderr": "",
            "returncode": -1,
            "elapsed_ms": 0,
        }
    import time
    t0 = time.time()
    try:
        proc = subprocess.run(
            [str(tool_path)] + [str(a) for a in args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None, timeout=timeout,
            **quiet_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "error": f"timeout after {timeout}s",
            "stdout": "", "stderr": "", "returncode": -1,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    except Exception as e:
        return {
            "ok": False, "error": f"{type(e).__name__}: {e}",
            "stdout": "", "stderr": "", "returncode": -1,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }
    elapsed = int((time.time() - t0) * 1000)
    return {
        "ok": proc.returncode == 0,
        "stdout": (proc.stdout or b"").decode("utf-8", errors="replace"),
        "stderr": (proc.stderr or b"").decode("utf-8", errors="replace"),
        "returncode": proc.returncode,
        "elapsed_ms": elapsed,
        "error": "" if proc.returncode == 0 else f"exit {proc.returncode}",
    }
