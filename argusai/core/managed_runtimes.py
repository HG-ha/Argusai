"""Verified, on-demand Python, Node, and WABT toolchains for assistant sandboxes.

The application itself never executes from these runtimes.  They are shared
base toolchains under ``data_root/tools/runtimes``; Python creates one venv per
assistant session and Node runs with a session-specific cwd/HOME/cache.

WABT is not a sandbox runtime — it is a set of WASM CLI tools — but it rides
the same install machinery so that WASM analysis stops costing 25 MB in every
download for the majority of users who never touch a ``.wasm`` file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from argusai.core.infra.win_stdio import quiet_subprocess_kwargs
from argusai.core.paths import PathManager

log = logging.getLogger("argusai.managed_runtimes")

RuntimeName = Literal["python", "node", "wabt"]

PYTHON_VERSION = "3.12.7"
PYTHON_BUILD_TAG = "20241016"
NODE_VERSION = "22.20.0"
WABT_VERSION = "1.0.36"
WEBCRACK_VERSION = "2.16.0"
RESTRINGER_VERSION = "2.0.0"

_USER_AGENT = "ArgusAI-managed-runtime/1.0"
_LOCK_TIMEOUT_SECONDS = 20 * 60
_LOCK_STALE_SECONDS = 60 * 60
_DOWNLOAD_RETRIES = 2
_NPM_INSTALL_RETRIES = 2
_CLEANUP_RETRIES = 5
_CONNECT_TIMEOUT_SECONDS = 20
_METADATA_FILE = ".argusai-runtime.json"


class RuntimeInstallError(RuntimeError):
    """Base class for a managed-runtime installation failure."""


class ChecksumMismatch(RuntimeInstallError):
    """A downloaded archive did not match the pinned trust anchor."""


class NonArchivePayload(RuntimeInstallError):
    """A source answered 200 with something that is not an archive at all."""


@dataclass(frozen=True)
class ArchiveSpec:
    runtime: RuntimeName
    version: str
    platform: str
    filename: str
    sha256: str
    approx_download_bytes: int


_PYTHON_TARGETS: dict[str, tuple[str, str]] = {
    "win-x64": (
        "x86_64-pc-windows-msvc",
        "fa8ac308a7cd1774d599ad9a29f1e374fbdc11453b12a8c50cc4afdb5c4bfd1a",
    ),
    "linux-x64": (
        "x86_64-unknown-linux-gnu",
        "3a4d53a7ba3916c0c1f35cbbe57068e2571b138389f29cf5c35367fec8f4c617",
    ),
    "linux-arm64": (
        "aarch64-unknown-linux-gnu",
        "c8f5ed70ee3c19da72d117f7b306adc6ca1eaf26afcbe1cc1be57d1e18df184c",
    ),
    "macos-x64": (
        "x86_64-apple-darwin",
        "848405b92bda20fad1f9bba99234c7d3f11e0b31e46f89835d1cb3d735e932aa",
    ),
    "macos-arm64": (
        "aarch64-apple-darwin",
        "95dd397e3aef4cc1846867cf20be704bdd74edd16ea8032caf01e48f0c53d65d",
    ),
}

_NODE_TARGETS: dict[str, tuple[str, str]] = {
    "win-x64": (
        f"node-v{NODE_VERSION}-win-x64.zip",
        "bb819d6eb8f5bfda294bbc83a7e4ec6539da67c4233d54b0d655b9248b15e29d",
    ),
    "win-arm64": (
        f"node-v{NODE_VERSION}-win-arm64.zip",
        "b12919e609b4fa1176ba8a155b49f761419a0c7cc97b42e6be09874a3f760ab6",
    ),
    "linux-x64": (
        f"node-v{NODE_VERSION}-linux-x64.tar.xz",
        "00bbd05e306ea68b6e13e17360d0e2f680b493ef95f2fea1c4296ff7437530bc",
    ),
    "linux-arm64": (
        f"node-v{NODE_VERSION}-linux-arm64.tar.xz",
        "06907b9c088ce62305bc1530e5c1ae1510245114645768f7750c349c5b6fe667",
    ),
    "macos-x64": (
        f"node-v{NODE_VERSION}-darwin-x64.tar.gz",
        "00df9c5df3e4ec6848c26b70fb47bf96492f342f4bed6b17f12d99b3a45eeecc",
    ),
    "macos-arm64": (
        f"node-v{NODE_VERSION}-darwin-arm64.tar.gz",
        "cc04a76a09f79290194c0646f48fec40354d88969bec467789a5d55dd097f949",
    ),
}


# WABT publishes exactly four binaries per release. There is no linux-arm64
# build, and macos-12/macos-14 are the x64/arm64 halves rather than two macOS
# versions of the same binary — mapping every macOS arch onto one of them
# hands Intel Macs an arm64 toolchain.
_WABT_TARGETS: dict[str, tuple[str, str]] = {
    "win-x64": (
        f"wabt-{WABT_VERSION}-windows.tar.gz",
        "406f9650ee3a6181c36f336d212c1fbba80cfcd99436bfcfad1da1109e1f344f",
    ),
    "linux-x64": (
        f"wabt-{WABT_VERSION}-ubuntu-20.04.tar.gz",
        "4aa0929db06c2376cc76f6d9e920dbde2ea6f1fd55bbaed6cd65167e9e39ecd3",
    ),
    "macos-x64": (
        f"wabt-{WABT_VERSION}-macos-12.tar.gz",
        "fe819b43cb13e5079c625135f22f9be584b0d657b57e9e048dc9fb8ffaa3ebd0",
    ),
    "macos-arm64": (
        f"wabt-{WABT_VERSION}-macos-14.tar.gz",
        "7cfdf12aa3fb8dadb6d9657b2281628f14c7071341ac0a4ca303086148bfb8d0",
    ),
}


def platform_subdir(system: str | None = None, machine: str | None = None) -> str:
    """Return the stable runtime platform key used by manifests and tests."""
    system_name = (system or platform.system()).strip().lower()
    machine_name = (machine or platform.machine()).strip().lower()
    if system_name.startswith("win"):
        os_name = "win"
    elif system_name in {"darwin", "mac", "macos"}:
        os_name = "macos"
    elif system_name == "linux":
        os_name = "linux"
    else:
        raise RuntimeInstallError(f"unsupported operating system: {system_name or '(empty)'}")
    if machine_name in {"x86_64", "amd64"}:
        arch = "x64"
    elif machine_name in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        raise RuntimeInstallError(f"unsupported architecture: {machine_name or '(empty)'}")
    return f"{os_name}-{arch}"


def archive_spec(runtime: str, *, platform_key: str | None = None) -> ArchiveSpec:
    key = str(runtime or "").strip().lower()
    plat = platform_key or platform_subdir()
    if key == "python":
        target = _PYTHON_TARGETS.get(plat)
        if target is None:
            raise RuntimeInstallError(f"managed Python {PYTHON_VERSION} is not available for {plat}")
        triple, digest = target
        filename = (
            f"cpython-{PYTHON_VERSION}+{PYTHON_BUILD_TAG}-{triple}"
            "-install_only_stripped.tar.gz"
        )
        return ArchiveSpec("python", PYTHON_VERSION, plat, filename, digest, 35 * 1024 * 1024)
    if key == "node":
        target = _NODE_TARGETS.get(plat)
        if target is None:
            raise RuntimeInstallError(f"managed Node {NODE_VERSION} is not available for {plat}")
        filename, digest = target
        return ArchiveSpec("node", NODE_VERSION, plat, filename, digest, 32 * 1024 * 1024)
    if key == "wabt":
        target = _WABT_TARGETS.get(plat)
        if target is None:
            raise RuntimeInstallError(f"managed WABT {WABT_VERSION} is not available for {plat}")
        filename, digest = target
        return ArchiveSpec("wabt", WABT_VERSION, plat, filename, digest, 5 * 1024 * 1024)
    raise ValueError("runtime must be 'python', 'node', or 'wabt'")


def managed_runtimes_root(tools_dir: str | Path | None = None) -> Path:
    base = Path(tools_dir).expanduser() if tools_dir is not None else PathManager().tools_dir
    return (base / "runtimes").resolve()


def runtime_root(runtime: str, tools_dir: str | Path | None = None) -> Path:
    key = str(runtime or "").strip().lower()
    if key not in {"python", "node", "wabt"}:
        raise ValueError("runtime must be 'python', 'node', or 'wabt'")
    return managed_runtimes_root(tools_dir) / key


def tools_dir_from_sandbox(sandbox_root: str | Path) -> Path:
    """Derive ``data_root/tools`` from ``data_root/assistant_sessions/<id>.sandbox``."""
    root = Path(sandbox_root).expanduser().resolve()
    if root.parent.name != "assistant_sessions":
        raise ValueError(f"sandbox is not under assistant_sessions: {root}")
    return root.parent.parent / "tools"


def vendor_assets_dir() -> Path:
    """Resolve ``argusai/vendor`` for shipped data files.

    Dev / Folder / OneFile keep vendor next to the package.  macOS ``.app``
    builds (see ``build.py``) place retained vendor
    trees under ``Contents/Resources/argusai/vendor`` so codesign does not
    treat npm metadata under ``Contents/MacOS`` as nested code.
    """
    package_root = Path(__file__).resolve().parent.parent  # …/argusai
    direct = package_root / "vendor"
    if sys.platform == "darwin":
        macos_dir = package_root.parent
        if macos_dir.name == "MacOS":
            bundled = macos_dir.parent / "Resources" / "argusai" / "vendor"
            if bundled.is_dir():
                return bundled
    return direct


def _runner_assets_dir() -> Path:
    return vendor_assets_dir() / "runners"


def node_tools_lockfile() -> Path:
    return _runner_assets_dir() / "package-lock.json"


def node_tools_package_file() -> Path:
    return _runner_assets_dir() / "package.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return total
    for current, _, files in os.walk(path):
        for filename in files:
            try:
                total += (Path(current) / filename).stat().st_size
            except OSError:
                continue
    return total


def _metadata(path: Path) -> dict[str, Any]:
    try:
        value = json.loads((path / _METADATA_FILE).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def python_executable(tools_dir: str | Path | None = None) -> Path:
    root = runtime_root("python", tools_dir)
    candidates = (
        root / "python.exe",
        root / "bin" / "python3",
        root / "bin" / f"python{PYTHON_VERSION.rsplit('.', 1)[0]}",
        root / "bin" / "python",
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def python_venv_health(
    runtime_dir: str | Path,
    *,
    platform_key: str | None = None,
) -> dict[str, Any]:
    """Verify the files required to create a session venv.

    POSIX venvs reuse or symlink the base interpreter. Windows uses dedicated
    launchers and templates which some otherwise runnable standalone archives
    omit, so a version probe alone is not a sufficient health check.
    """
    root = Path(runtime_dir).expanduser().resolve()
    plat = platform_key or platform_subdir()
    if not plat.startswith("win-"):
        return {"ok": True, "platform": plat, "runtime_dir": str(root), "missing": []}
    required = (
        root / "python.exe",
        root / "Lib" / "venv" / "scripts" / "nt" / "python.exe",
        root / "Lib" / "venv" / "scripts" / "nt" / "pythonw.exe",
    )
    has_stdlib = (
        (root / "Lib" / "struct.py").is_file()
        or any(path.is_file() for path in root.glob("python*.zip"))
    )
    missing = [str(path) for path in required if not path.is_file()]
    if not has_stdlib:
        missing.append(str(root / "Lib" / "struct.py"))
    return {
        "ok": not missing,
        "platform": plat,
        "runtime_dir": str(root),
        "missing": missing,
    }


def node_executable(tools_dir: str | Path | None = None) -> Path:
    root = runtime_root("node", tools_dir)
    candidates = (
        root / ("node.exe" if os.name == "nt" else "node"),
        root / "bin" / "node",
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def node_command(name: str, tools_dir: str | Path | None = None) -> Path:
    root = runtime_root("node", tools_dir)
    command = str(name or "").strip().lower()
    if command == "node":
        return node_executable(tools_dir)
    if os.name == "nt":
        candidates = (root / f"{command}.cmd", root / f"{command}.exe", root / command)
    else:
        candidates = (root / "bin" / command, root / command)
    return next((path for path in candidates if path.is_file()), candidates[0])


def node_tools_dir(tools_dir: str | Path | None = None) -> Path:
    return runtime_root("node", tools_dir) / "tools"


def wabt_bin_dir(tools_dir: str | Path | None = None) -> Path:
    return runtime_root("wabt", tools_dir) / "bin"


def wabt_executable(name: str = "wasm-decompile", tools_dir: str | Path | None = None) -> Path:
    """Path to one WABT CLI tool; all of them live side by side in ``bin``."""
    suffix = ".exe" if os.name == "nt" else ""
    return wabt_bin_dir(tools_dir) / f"{str(name or 'wasm-decompile').strip()}{suffix}"


def _probe_version(argv: list[str], *, timeout: int = 10) -> tuple[bool, str, str]:
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            text=False,
            env=_clean_runtime_env(),
            **quiet_subprocess_kwargs(),
        )
    except Exception as exc:
        return False, "", f"{type(exc).__name__}: {exc}"
    output = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", errors="replace").strip()
    return proc.returncode == 0, output.splitlines()[0][:300] if output else "", (
        "" if proc.returncode == 0 else f"version probe exited {proc.returncode}: {output[:500]}"
    )


def runtime_status(runtime: str, tools_dir: str | Path | None = None) -> dict[str, Any]:
    """Return a JSON-clean health snapshot without installing anything."""
    key = str(runtime or "").strip().lower()
    try:
        spec = archive_spec(key)
    except RuntimeInstallError as exc:
        return {
            "ok": True,
            "runtime": key,
            "installed": False,
            "healthy": False,
            "status": "unsupported",
            "version": "",
            "expected_version": {
                "python": PYTHON_VERSION,
                "node": NODE_VERSION,
                "wabt": WABT_VERSION,
            }.get(key, ""),
            "platform": platform_subdir(),
            "root": str(runtime_root(key, tools_dir)),
            "executable": "",
            "size_bytes": 0,
            "download_size_bytes": 0,
            "source": "",
            "download_attempts": [],
            "npm_source": "",
            "npm_attempts": [],
            "installed_at": None,
            "archive_sha256": "",
            "error": str(exc),
            "packages": {},
            "capabilities": {},
        }
    root = runtime_root(spec.runtime, tools_dir)
    metadata = _metadata(root)
    if spec.runtime == "python":
        executable = python_executable(tools_dir)
    elif spec.runtime == "wabt":
        executable = wabt_executable(tools_dir=tools_dir)
    else:
        executable = node_executable(tools_dir)
    exists = root.is_dir()
    version = ""
    error = ""
    healthy = False
    if executable.is_file():
        ok, output, error = _probe_version([str(executable), "--version"])
        version = output
        if ok:
            expected = spec.version
            healthy = expected in output
            if not healthy:
                error = f"expected {expected}, got {output or '(empty version)'}"
    elif exists:
        error = f"runtime executable missing: {executable}"
    else:
        error = "managed runtime is not installed"

    capabilities: dict[str, Any] = {}
    if spec.runtime == "python" and healthy:
        venv_health = python_venv_health(root, platform_key=spec.platform)
        capabilities["venv"] = venv_health
        if not venv_health.get("ok"):
            healthy = False
            error = "managed Python is incomplete for session venv creation"

    installed_sha256 = str(metadata.get("archive_sha256") or "")
    if healthy and installed_sha256 != spec.sha256:
        healthy = False
        error = "managed runtime archive pin changed; repair is required"

    packages: dict[str, Any] = {}
    if spec.runtime == "node" and healthy:
        expected_packages = {
            "webcrack": WEBCRACK_VERSION,
            "restringer": RESTRINGER_VERSION,
        }
        for name, expected in expected_packages.items():
            package_json = node_tools_dir(tools_dir) / "node_modules" / name / "package.json"
            actual = ""
            try:
                actual = str(json.loads(package_json.read_text(encoding="utf-8")).get("version") or "")
            except Exception:
                pass
            package_ok = actual == expected
            packages[name] = {
                "found": package_json.is_file(),
                "version": actual,
                "expected_version": expected,
                "healthy": package_ok,
                "path": str(package_json.parent),
            }
            if not package_ok:
                healthy = False
                error = f"{name} {expected} is missing or damaged"
        lock_path = node_tools_lockfile()
        lock_sha = _sha256(lock_path) if lock_path.is_file() else ""
        if not lock_sha or metadata.get("lock_sha256") != lock_sha:
            healthy = False
            error = "managed Node dependency lockfile changed; repair is required"

    return {
        "ok": True,
        "runtime": spec.runtime,
        "installed": exists,
        "healthy": healthy,
        "status": "ready" if healthy else ("damaged" if exists else "missing"),
        "version": version,
        "expected_version": spec.version,
        "platform": spec.platform,
        "root": str(root),
        "executable": str(executable) if executable.is_file() else "",
        "size_bytes": _dir_size(root),
        "download_size_bytes": spec.approx_download_bytes,
        "source": str(metadata.get("source") or ""),
        "download_attempts": list(metadata.get("download_attempts") or []),
        "npm_source": str(metadata.get("npm_source") or ""),
        "npm_attempts": list(metadata.get("npm_attempts") or []),
        "installed_at": metadata.get("installed_at"),
        "archive_sha256": installed_sha256,
        "error": error,
        "packages": packages,
        "capabilities": capabilities,
    }


def all_runtime_status(tools_dir: str | Path | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "root": str(managed_runtimes_root(tools_dir)),
        "python": runtime_status("python", tools_dir),
        "node": runtime_status("node", tools_dir),
        "wabt": runtime_status("wabt", tools_dir),
    }


def runtime_kind(runtime: str) -> str:
    """WABT ships CLI binaries; Python and Node back per-session sandboxes."""
    return "toolchain" if runtime == "wabt" else "sandbox environment"


def managed_runtime_required(runtime: str, tools_dir: str | Path | None = None) -> dict[str, Any]:
    status = runtime_status(runtime, tools_dir)
    kind = runtime_kind(status["runtime"])
    return {
        "ok": False,
        "blocked": True,
        "error_code": "managed_runtime_required",
        "runtime": status["runtime"],
        "runtime_status": status,
        "error": f"{status['runtime']} {kind} is {status['status']}",
        "retryable": True,
        "suggested_fix": (
            f"Approve installation of the {status['runtime']} {kind}, "
            "or install/repair it from Settings → Sandbox runtimes."
        ),
    }


def _mirror_values(mirrors: Mapping[str, Any] | None, key: str) -> list[str]:
    if not mirrors:
        return []
    raw = mirrors.get(key)
    if isinstance(raw, str):
        values = re.split(r"[\r\n,;]+", raw)
    elif isinstance(raw, Sequence):
        values = [str(item) for item in raw]
    else:
        values = []
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def _mirror_url(base: str, spec: ArchiveSpec, *, release_prefix: str = "") -> str:
    encoded = quote(spec.filename, safe="-_.")
    value = str(base or "").strip()
    if "{filename}" in value or "{version}" in value or "{release}" in value:
        return value.format(filename=encoded, version=spec.version, release=PYTHON_BUILD_TAG)
    lowered = value.lower()
    if lowered.endswith((".zip", ".tar.gz", ".tgz", ".tar.xz")):
        return value
    suffix = f"{release_prefix.strip('/')}/" if release_prefix else ""
    return f"{value.rstrip('/')}/{suffix}{encoded}"


def _release_prefix(runtime: str) -> str:
    if runtime == "python":
        return PYTHON_BUILD_TAG
    if runtime == "wabt":
        return WABT_VERSION
    return f"v{NODE_VERSION}"


# Reverse proxies that re-serve github.com release assets from inside China.
# Each one was accepted only after a full download of both a 5 MB and a 35 MB
# release asset hashed to the pinned SHA-256: several popular endpoints answer
# HTTP 200 with an HTML error page, which a liveness ping would not catch.
# Ordered by measured throughput. They are fallbacks, not trust anchors --
# every byte still has to match the pinned digest.
_GITHUB_ACCELERATORS: tuple[str, ...] = (
    "https://cors.isteed.cc/",
    "https://gh.llkk.cc/",
    "https://gh-proxy.com/",
    "https://gh.ddlc.top/",
    "https://gh.xxooo.cf/",
    "https://ghfast.top/",
    "https://ghproxy.net/",
)


def github_release_sources(repo: str, tag: str, filename: str) -> list[str]:
    """The upstream release URL followed by China-friendly proxies of it."""
    upstream = f"https://github.com/{repo}/releases/download/{tag}/{filename}"
    return [upstream, *(f"{prefix}{upstream}" for prefix in _GITHUB_ACCELERATORS)]


def archive_sources(spec: ArchiveSpec, mirrors: Mapping[str, Any] | None = None) -> list[str]:
    custom = [
        _mirror_url(base, spec, release_prefix=_release_prefix(spec.runtime))
        for base in _mirror_values(mirrors, spec.runtime)
    ]
    encoded = quote(spec.filename, safe="-_.")
    if spec.runtime == "python":
        builtins = github_release_sources(
            "astral-sh/python-build-standalone", PYTHON_BUILD_TAG, encoded
        )
    elif spec.runtime == "wabt":
        builtins = github_release_sources("WebAssembly/wabt", WABT_VERSION, encoded)
    else:
        builtins = [
            f"https://nodejs.org/dist/v{NODE_VERSION}/{encoded}",
            f"https://npmmirror.com/mirrors/node/v{NODE_VERSION}/{encoded}",
            f"https://mirrors.huaweicloud.com/nodejs/v{NODE_VERSION}/{encoded}",
            f"https://mirrors.aliyun.com/nodejs-release/v{NODE_VERSION}/{encoded}",
            f"https://mirrors.ustc.edu.cn/node/v{NODE_VERSION}/{encoded}",
        ]
    return list(dict.fromkeys([*custom, *builtins]))


def npm_registries(mirrors: Mapping[str, Any] | None = None) -> list[str]:
    return list(dict.fromkeys([
        *[value.rstrip("/") for value in _mirror_values(mirrors, "npm")],
        "https://registry.npmjs.org",
        "https://registry.npmmirror.com",
    ]))


class _InstallLock(AbstractContextManager["_InstallLock"]):
    def __init__(self, path: Path, *, timeout: float = _LOCK_TIMEOUT_SECONDS) -> None:
        self.path = path
        self.timeout = timeout
        self.acquired = False

    def __enter__(self) -> "_InstallLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"pid": os.getpid(), "created_at": time.time()}, fh)
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                    if age > _LOCK_STALE_SECONDS:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeInstallError(f"timed out waiting for runtime install lock: {self.path}")
                time.sleep(0.2)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.acquired:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


def _tls_context() -> ssl.SSLContext:
    """Trust store for runtime downloads.

    macOS Pythons, including a Nuitka build, often have an empty default CA
    list (``CERTIFICATE_VERIFY_FAILED: unable to get local issuer``). certifi
    ships a Mozilla bundle that does not depend on the system keychain.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        log.debug("certifi CA bundle unavailable; using the default SSL context", exc_info=True)
        return ssl.create_default_context()


def _download_once(url: str, destination: Path, *, timeout: int = _CONNECT_TIMEOUT_SECONDS) -> None:
    request = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/octet-stream"})
    with urlopen(request, timeout=timeout, context=_tls_context()) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 256)


_ARCHIVE_MAGIC: tuple[bytes, ...] = (
    b"\x1f\x8b",      # gzip
    b"PK\x03\x04",    # zip
    b"PK\x05\x06",    # empty zip
    b"\xfd7zXZ\x00",  # xz
    b"BZh",           # bzip2
)


def _looks_like_archive(path: Path) -> bool:
    """Tell a proxy's HTML error page apart from a real (if wrong) archive."""
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
            handle.seek(257)  # POSIX tar puts its magic here
            ustar = handle.read(5)
    except OSError:
        return False
    return ustar == b"ustar" or any(head.startswith(magic) for magic in _ARCHIVE_MAGIC)


def _fallback_allowed(exc: BaseException) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code == 404 or exc.code >= 500
    if isinstance(exc, NonArchivePayload):
        return True
    return isinstance(exc, (URLError, TimeoutError, ConnectionError))


def download_verified(
    urls: Sequence[str],
    destination: Path,
    expected_sha256: str,
    *,
    downloader: Callable[[str, Path], None] | None = None,
) -> dict[str, Any]:
    """Download in source order and stop immediately on an integrity failure."""
    attempts: list[dict[str, Any]] = []
    destination.parent.mkdir(parents=True, exist_ok=True)
    fetch = downloader or (lambda url, path: _download_once(url, path))
    for url in urls:
        for retry in range(_DOWNLOAD_RETRIES):
            destination.unlink(missing_ok=True)
            started = time.perf_counter()
            try:
                fetch(url, destination)
                actual = _sha256(destination)
                elapsed = int((time.perf_counter() - started) * 1000)
                if actual.lower() != expected_sha256.lower():
                    # A mirror that answers 200 with an HTML error page never
                    # delivered the asset, so it is an unusable source rather
                    # than evidence of tampering. Anything shaped like an
                    # archive is still treated as a hard integrity failure.
                    if not _looks_like_archive(destination):
                        raise NonArchivePayload(
                            f"{destination.stat().st_size} bytes that are not an archive"
                        )
                    destination.unlink(missing_ok=True)
                    raise ChecksumMismatch(
                        f"SHA-256 mismatch from {url}: expected {expected_sha256}, got {actual}"
                    )
                attempts.append({"url": url, "ok": True, "duration_ms": elapsed, "retry": retry})
                return {"source": url, "sha256": actual, "attempts": attempts}
            except ChecksumMismatch:
                raise
            except Exception as exc:
                elapsed = int((time.perf_counter() - started) * 1000)
                attempts.append({
                    "url": url,
                    "ok": False,
                    "duration_ms": elapsed,
                    "retry": retry,
                    "error": f"{type(exc).__name__}: {exc}",
                    **({"status": exc.code} if isinstance(exc, HTTPError) else {}),
                })
                destination.unlink(missing_ok=True)
                if not _fallback_allowed(exc):
                    raise RuntimeInstallError(f"download rejected by {url}: {exc}") from exc
                if retry + 1 < _DOWNLOAD_RETRIES:
                    time.sleep(0.25 * (2**retry))
        log.info("runtime download source exhausted url=%s attempts=%s", url, attempts[-_DOWNLOAD_RETRIES:])
    raise RuntimeInstallError(f"all runtime download sources failed: {attempts}")


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for item in bundle.infolist():
                target = (root / item.filename).resolve()
                if target != root and root not in target.parents:
                    raise RuntimeInstallError(f"archive path traversal blocked: {item.filename}")
            bundle.extractall(root)
        return
    try:
        with tarfile.open(archive, "r:*") as bundle:
            # Python 3.12's data filter rejects absolute paths, escaping
            # symlink targets, devices, and unsafe permission bits.
            bundle.extractall(root, filter="data")
    except (tarfile.TarError, zipfile.BadZipFile) as exc:
        raise RuntimeInstallError(f"invalid runtime archive: {exc}") from exc


def _single_payload_root(extracted: Path) -> Path:
    entries = [entry for entry in extracted.iterdir() if entry.name not in {"__MACOSX"}]
    return entries[0] if len(entries) == 1 and entries[0].is_dir() else extracted


def _clean_runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__"):
        env.pop(key, None)
    env.setdefault("PYTHONUTF8", "1")
    return env


def _node_npm_cli(node_root: Path) -> Path:
    candidates = (
        node_root / "node_modules" / "npm" / "bin" / "npm-cli.js",
        node_root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js",
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def _npm_can_fallback(output: str) -> bool:
    lowered = output.lower()
    if "eintegrity" in lowered or "integrity checksum failed" in lowered:
        raise ChecksumMismatch(f"npm lockfile integrity check failed: {output[-2000:]}")
    retry_markers = (
        "econnreset", "econnrefused", "etimedout", "eai_again", "enotfound",
        "network", "eperm", "ebusy", "404 not found", "502 bad gateway",
        "503 service unavailable", "504 gateway timeout",
    )
    return any(marker in lowered for marker in retry_markers)


def _npm_can_retry_same_registry(output: str) -> bool:
    lowered = output.lower()
    transient_markers = (
        "econnreset",
        "econnrefused",
        "etimedout",
        "eai_again",
        "enotfound",
        "network",
        "eperm",
        "ebusy",
        "502 bad gateway",
        "503 service unavailable",
        "504 gateway timeout",
    )
    return any(marker in lowered for marker in transient_markers)


def _remove_tree_with_retries(path: Path, *, retries: int = _CLEANUP_RETRIES) -> bool:
    """Best-effort Windows-friendly cleanup for native npm artifacts."""
    if not path.exists():
        return True
    for attempt in range(max(1, retries)):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            return True
        except OSError as exc:
            if attempt + 1 >= retries:
                log.warning("runtime staging cleanup failed path=%s error=%s", path, exc)
                return False
            time.sleep(0.15 * (attempt + 1))
        else:
            return True
    return not path.exists()


def _cleanup_stale_staging(runtimes: Path, runtime: str) -> None:
    """Remove leftovers from interrupted installs while holding the install lock."""
    for path in runtimes.glob(f".staging-{runtime}-*"):
        if path.is_dir():
            _remove_tree_with_retries(path)


def _npm_error_summary(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    errors = [
        line
        for line in lines
        if "npm error" in line.lower() or "npm err!" in line.lower()
    ]
    selected = errors[-4:] or lines[-4:]
    return " | ".join(selected)[-1200:]


def _install_node_packages(
    node_root: Path,
    staging_root: Path,
    mirrors: Mapping[str, Any] | None,
) -> dict[str, Any]:
    package_file = node_tools_package_file()
    lock_file = node_tools_lockfile()
    if not package_file.is_file() or not lock_file.is_file():
        raise RuntimeInstallError(
            "managed Node package.json/package-lock.json is missing "
            f"(looked under {_runner_assets_dir()})"
        )
    node = next(
        (path for path in (node_root / "node.exe", node_root / "bin" / "node") if path.is_file()),
        node_root / "node.exe",
    )
    npm_cli = _node_npm_cli(node_root)
    if not node.is_file() or not npm_cli.is_file():
        raise RuntimeInstallError(f"npm is missing from the Node archive: node={node}, npm={npm_cli}")

    attempts: list[dict[str, Any]] = []
    install_dir = node_root / "tools"
    for registry_index, registry in enumerate(npm_registries(mirrors)):
        for retry in range(_NPM_INSTALL_RETRIES):
            # A failed native-module install can leave DLLs briefly locked on
            # Windows. Never reuse that directory or cache for another mirror.
            attempt_dir = staging_root / f"npm-attempt-{registry_index}-{retry}"
            attempt_dir.mkdir(parents=True, exist_ok=False)
            shutil.copy2(package_file, attempt_dir / "package.json")
            shutil.copy2(lock_file, attempt_dir / "package-lock.json")

            env = _clean_runtime_env()
            env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
            env["npm_config_registry"] = registry
            env["npm_config_replace_registry_host"] = "always"
            env["npm_config_cache"] = str(
                staging_root / f"npm-cache-{registry_index}-{retry}"
            )
            started = time.perf_counter()
            proc = subprocess.run(
                [
                    str(node),
                    str(npm_cli),
                    "ci",
                    "--no-audit",
                    "--no-fund",
                    "--no-update-notifier",
                ],
                cwd=str(attempt_dir),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15 * 60,
                text=False,
                **quiet_subprocess_kwargs(),
            )
            output = ((proc.stdout or b"") + (proc.stderr or b"")).decode(
                "utf-8",
                errors="replace",
            )
            attempt = {
                "registry": registry,
                "retry": retry,
                "ok": proc.returncode == 0,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "returncode": proc.returncode,
                "output": output[-4000:],
            }
            attempts.append(attempt)
            if proc.returncode == 0:
                if install_dir.exists() and not _remove_tree_with_retries(install_dir):
                    raise RuntimeInstallError(
                        f"cannot replace managed Node tools directory: {install_dir}"
                    )
                os.replace(attempt_dir, install_dir)
                return {"source": registry, "attempts": attempts}

            can_fallback = _npm_can_fallback(output)
            can_retry = _npm_can_retry_same_registry(output)
            _remove_tree_with_retries(attempt_dir)
            if not can_fallback:
                raise RuntimeInstallError(
                    f"npm ci failed via {registry}: {_npm_error_summary(output)}"
                )
            if not can_retry or retry + 1 >= _NPM_INSTALL_RETRIES:
                break
            time.sleep(0.25 * (retry + 1))

    summaries = [
        f"{item['registry']}#{int(item['retry']) + 1}: "
        f"{_npm_error_summary(str(item.get('output') or 'unknown error'))}"
        for item in attempts
    ]
    raise RuntimeInstallError(
        "all npm registries failed: " + "; ".join(summaries)[-3000:]
    )


def _atomic_replace(payload: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = destination.parent / f".backup-{destination.name}-{uuid.uuid4().hex}"
    had_destination = destination.exists()
    try:
        if had_destination:
            os.replace(destination, backup)
        os.replace(payload, destination)
    except Exception:
        if had_destination and backup.exists() and not destination.exists():
            os.replace(backup, destination)
        raise
    else:
        shutil.rmtree(backup, ignore_errors=True)


def install_runtime(
    runtime: str,
    *,
    tools_dir: str | Path | None = None,
    force: bool = False,
    mirrors: Mapping[str, Any] | None = None,
    downloader: Callable[[str, Path], None] | None = None,
) -> dict[str, Any]:
    """Install or repair one runtime with locking and atomic replacement."""
    try:
        spec = archive_spec(runtime)
    except (ValueError, RuntimeInstallError) as exc:
        return {"ok": False, "runtime": str(runtime or ""), "error": str(exc)}
    runtimes = managed_runtimes_root(tools_dir)
    destination = runtime_root(spec.runtime, tools_dir)
    started = time.perf_counter()
    lock_path = runtimes / f".{spec.runtime}.install.lock"
    try:
        with _InstallLock(lock_path):
            current = runtime_status(spec.runtime, tools_dir)
            if current.get("healthy") and not force:
                return {**current, "ok": True, "already_installed": True}

            runtimes.mkdir(parents=True, exist_ok=True)
            _cleanup_stale_staging(runtimes, spec.runtime)
            staging = Path(tempfile.mkdtemp(prefix=f".staging-{spec.runtime}-", dir=runtimes))
            try:
                archive = staging / spec.filename
                downloaded = download_verified(
                    archive_sources(spec, mirrors),
                    archive,
                    spec.sha256,
                    downloader=downloader,
                )
                extracted = staging / "extracted"
                _safe_extract(archive, extracted)
                payload = _single_payload_root(extracted)
                npm_result: dict[str, Any] = {}
                if spec.runtime == "node":
                    npm_result = _install_node_packages(payload, staging, mirrors)

                if spec.runtime == "python":
                    candidates = (payload / "python.exe", payload / "bin" / "python3", payload / "bin" / "python")
                elif spec.runtime == "wabt":
                    suffix = ".exe" if os.name == "nt" else ""
                    candidates = (payload / "bin" / f"wasm-decompile{suffix}",)
                else:
                    candidates = (payload / "node.exe", payload / "bin" / "node")
                executable = next((p for p in candidates if p.is_file()), candidates[0])
                ok, version, probe_error = _probe_version([str(executable), "--version"])
                if not ok or spec.version not in version:
                    raise RuntimeInstallError(
                        f"installed {spec.runtime} failed verification: {probe_error or version}"
                    )
                metadata = {
                    "runtime": spec.runtime,
                    "version": spec.version,
                    "platform": spec.platform,
                    "source": downloaded["source"],
                    "archive_sha256": downloaded["sha256"],
                    "installed_at": time.time(),
                    "download_attempts": downloaded["attempts"],
                }
                if spec.runtime == "node":
                    metadata.update({
                        "npm_source": npm_result.get("source", ""),
                        "npm_attempts": npm_result.get("attempts", []),
                        "lock_sha256": _sha256(node_tools_lockfile()),
                        "packages": {
                            "webcrack": WEBCRACK_VERSION,
                            "restringer": RESTRINGER_VERSION,
                        },
                    })
                (payload / _METADATA_FILE).write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _atomic_replace(payload, destination)
            finally:
                _remove_tree_with_retries(staging)
        result = runtime_status(spec.runtime, tools_dir)
        result.update({
            "ok": bool(result.get("healthy")),
            "installed_now": True,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        })
        return result
    except ChecksumMismatch as exc:
        log.error("runtime checksum failure runtime=%s error=%s", spec.runtime, exc)
        return {
            "ok": False,
            "runtime": spec.runtime,
            "error_code": "runtime_checksum_mismatch",
            "error": str(exc),
            "retryable": False,
            "root": str(destination),
        }
    except Exception as exc:
        log.warning("runtime install failed runtime=%s error=%s", spec.runtime, exc)
        return {
            "ok": False,
            "runtime": spec.runtime,
            "error_code": "runtime_install_failed",
            "error": f"{type(exc).__name__}: {exc}",
            "retryable": True,
            "root": str(destination),
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
