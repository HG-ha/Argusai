"""Desktop updates from a bounded website manifest and verified release assets.

No GitHub API or credentials are used. The GUI owns confirmation, busy-work
checks and orderly shutdown; this module only prepares an external installer.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlsplit

import httpx

MANIFEST_URL = "https://argusai.sh/updates/stable.json"
MAX_MANIFEST_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 2 * 1024**3
MAX_EXTRACTED_BYTES = 8 * 1024**3
CHECK_INTERVAL = 24 * 3600
SUFFIXES = {
    "win-x64": "win-x64-setup.exe", "win-arm64": "win-arm64-setup.exe",
    "linux-x64": "linux-x64.tar.gz", "linux-arm64": "linux-arm64.tar.gz",
    "macos-arm64": "macos-arm64.dmg",
}


def _version(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    if not match:
        raise ValueError("Invalid stable release version")
    return tuple(map(int, match.groups()))  # type: ignore[return-value]


def runtime_platform(system: str | None = None, machine: str | None = None,
                     executable: Path | None = None) -> str:
    """Match NSIS's native Windows architecture gate, including emulation."""
    system = system or platform.system()
    supplied_machine = machine
    machine = (machine or platform.machine()).lower()
    if system == "Windows" and supplied_machine is None:
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            process = wintypes.USHORT()
            native = wintypes.USHORT()
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            check = kernel.IsWow64Process2
            check.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT)]
            if not check(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
                raise OSError("Cannot determine native Windows architecture")
            machine = {0x8664: "amd64", 0xAA64: "arm64"}.get(native.value, "unsupported")
        except (OSError, AttributeError):
            machine = os.environ.get("PROCESSOR_ARCHITEW6432", os.environ.get("PROCESSOR_ARCHITECTURE", machine)).lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x64" if machine in {"x86_64", "amd64", "x64"} else "unsupported"
    return {"Windows": "win", "Linux": "linux", "Darwin": "macos"}.get(system, "unsupported") + "-" + arch


def _release_url(value: str, expected: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != expected):
        raise ValueError("Update URL must point to the matching HG-ha/Argusai release")
    return value


def validate_manifest(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unsupported update manifest")
    version = str(data.get("version", ""))
    _version(version)
    if version.startswith("v"):
        raise ValueError("Manifest version must not contain a tag prefix")
    notes = data.get("notes", "")
    if not isinstance(notes, str) or len(notes.encode("utf-8")) > 64 * 1024:
        raise ValueError("Invalid update release notes")
    release = _release_url(str(data.get("release_url", "")), f"/HG-ha/Argusai/releases/tag/v{version}")
    assets = data.get("assets")
    if not isinstance(assets, dict):
        raise ValueError("Missing update assets")
    validated = {}
    for key, suffix in SUFFIXES.items():
        if key not in assets:
            continue
        asset = assets[key]
        if not isinstance(asset, dict):
            raise ValueError("Invalid update asset")
        name = f"ArgusAI-{version}-{suffix}"
        url = _release_url(str(asset.get("url", "")), f"/HG-ha/Argusai/releases/download/v{version}/{name}")
        digest, size = asset.get("sha256"), asset.get("size")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
            raise ValueError("Missing or invalid update SHA-256")
        if type(size) is not int or not 0 < size < MAX_PACKAGE_BYTES:
            raise ValueError("Invalid update package size")
        validated[key] = {"url": url, "sha256": digest.lower(), "size": size, "name": name}
    return {"version": version, "notes": notes, "release_url": release,
            "published_at": str(data.get("published_at", ""))[:64], "assets": validated}


def verify_package(path: Path, asset: dict[str, Any]) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != asset["size"]:
        raise ValueError("Downloaded update size does not match the release")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != asset["sha256"]:
        raise ValueError("Downloaded update SHA-256 does not match the release")


def _target(executable: Path, key: str) -> Path:
    if key.startswith("macos-"):
        for path in executable.parents:
            if path.name == "ArgusAI.app":
                return path
        raise ValueError("Automatic updates require an installed ArgusAI.app")
    expected = "ArgusAI.exe" if key.startswith("win-") else "ArgusAI"
    if executable.name != expected:
        raise ValueError("Automatic updates require a packaged ArgusAI executable")
    parent = executable.parent
    if (parent.parent == parent or parent == Path.home().resolve()
            or parent.name.lower() in {"downloads", "desktop", "documents", "bin", "applications", "program files"}):
        raise ValueError("Automatic updates require a dedicated packaged ArgusAI directory")
    markers = [parent / "licenses/LICENSE"]
    if key.startswith("win-"):
        markers.extend([parent / "Uninstall.exe", parent / "launch-gui.vbs"])
    else:
        markers.append(parent / "licenses/SOURCE_CODE.txt")
    if any(not marker.is_file() or marker.is_symlink() for marker in markers):
        raise ValueError("Automatic updates require the complete packaged ArgusAI directory")
    return parent


def _safe_extract_linux(package: Path, stage: Path, version: str) -> Path:
    """Only expand regular files/directories and in-tree links from our archive."""
    expected_root = package.name.removesuffix(".tar.gz")
    if expected_root not in {f"ArgusAI-{version}-linux-x64", f"ArgusAI-{version}-linux-arm64"}:
        raise ValueError("Unexpected update archive name")
    with tarfile.open(package, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > 100000 or sum(m.size for m in members) > MAX_EXTRACTED_BYTES:
            raise ValueError("Update archive exceeds extraction limits")
        roots = set()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name or not path.parts:
                raise ValueError("Unsafe update archive path")
            roots.add(path.parts[0])
            if member.issym() or member.islnk():
                # Nuitka libraries may use relative SONAME links. Python's data
                # filter performs the actual resolved link containment check.
                pass
            elif not member.isfile() and not member.isdir():
                raise ValueError("Unsupported update archive entry")
        if roots != {expected_root}:
            raise ValueError("Unexpected update archive layout")
        archive.extractall(stage, members=members, filter="data")
    payload = stage / next(iter(roots))
    launcher = payload / "ArgusAI"
    if not launcher.is_file() or launcher.is_symlink():
        raise ValueError("Update archive has no ArgusAI launcher")
    launcher.chmod(launcher.stat().st_mode | 0o111)
    return payload


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _update_parent_pids() -> tuple[int, ...]:
    """Wait for both the application child and its onefile bootstrap."""
    process_ids = [os.getpid()]
    original_parent = os.environ.get("NUITKA_ONEFILE_PARENT", "")
    if re.fullmatch(r"[1-9][0-9]{0,9}", original_parent):
        parent = int(original_parent)
        if parent != process_ids[0]:
            process_ids.append(parent)
    return tuple(process_ids)


def _macos_launcher(bundle: Path) -> Path:
    """Resolve the actual Nuitka entry point recorded in the signed bundle."""
    metadata = bundle / "Contents/Info.plist"
    if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size > 128 * 1024:
        raise ValueError("Update bundle has invalid application metadata")
    with metadata.open("rb") as stream:
        info = plistlib.load(stream)
    name = info.get("CFBundleExecutable") if isinstance(info, dict) else None
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or any(char in name for char in ("/", "\\", "\x00"))):
        raise ValueError("Update bundle has an invalid executable name")
    directory = bundle / "Contents/MacOS"
    launcher = directory / name
    if launcher.is_symlink() or not launcher.is_file() or launcher.resolve().parent != directory.resolve():
        raise ValueError("Update bundle has no valid application executable")
    return launcher


class UpdateManager:
    def __init__(self, cache_dir: Path, current_version: str, *,
                 client: httpx.Client | None = None, platform_key: str | None = None,
                 executable: Path | None = None, packaged: bool | None = None,
                 protected_paths: Iterable[Path | str] = ()) -> None:
        _version(current_version)
        self.cache_dir = Path(cache_dir).resolve()
        self.current_version = current_version
        self.packaged = packaged if packaged is not None else bool(getattr(sys, "frozen", False) or "__compiled__" in globals())
        if executable is None and self.packaged:
            # A onefile child's sys.executable can live in its extraction cache.
            from argusai.core.infra.runtime_exec import frozen_self_executable

            executable = Path(frozen_self_executable())
        self.executable = Path(executable or sys.executable).resolve()
        self.platform_key = platform_key or runtime_platform(executable=self.executable)
        self.protected_paths = tuple(Path(p).resolve() for p in protected_paths if p)
        self._client = client
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._last_check: float | None = None
        self._manifest: dict[str, Any] | None = None
        self._package: Path | None = None
        self._state: dict[str, Any] = {
            "status": "idle", "current_version": current_version, "version": "",
            "notes": "", "release_url": "https://github.com/HG-ha/Argusai/releases",
            "platform": self.platform_key, "downloaded_bytes": 0, "total_bytes": 0,
            "error": "", "error_code": "", "install_mode": "automatic" if self.packaged else "source",
        }
        if self.platform_key not in SUFFIXES:
            self._state["install_mode"] = "manual"
        elif self.packaged:
            try:
                _target(self.executable, self.platform_key)
            except ValueError:
                self._state["install_mode"] = "manual"
        self._restore_ready()

    def _restore_ready(self) -> None:
        try:
            saved = self.cache_dir / "verified-update.json"
            if not self.packaged or not saved.is_file() or saved.stat().st_size > MAX_MANIFEST_BYTES:
                return
            manifest = validate_manifest(json.loads(saved.read_text(encoding="utf-8")))
            if _version(manifest["version"]) <= _version(self.current_version):
                return
            asset = manifest["assets"][self.platform_key]
            package = self.cache_dir / asset["name"]
            verify_package(package, asset)
            self._manifest, self._package = manifest, package
            self._state.update(status="downloaded", version=manifest["version"], notes=manifest["notes"],
                               release_url=manifest["release_url"], published_at=manifest["published_at"],
                               downloaded_bytes=asset["size"], total_bytes=asset["size"])
            report = self.cache_dir / "install-result.json"
            if report.is_file() and report.stat().st_size <= 16384:
                result = json.loads(report.read_text(encoding="utf-8-sig"))
                if (isinstance(result, dict) and result.get("ok") is False
                        and result.get("version") == manifest["version"]
                        and result.get("current_version") == self.current_version
                        and isinstance(result.get("error"), str)):
                    self._state.update(error=result["error"][:2048], error_code="install_failed")
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_ready(self) -> None:
        saved = self.cache_dir / "verified-update.json"
        temporary = self.cache_dir / "verified-update.json.tmp"
        record = {"schema_version": 1, **self._manifest}
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, saved)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _set(self, **values: Any) -> dict[str, Any]:
        with self._lock:
            self._state.update(values)
            return dict(self._state)

    def _http(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=httpx.Timeout(30, connect=10),
                                             headers={"User-Agent": f"ArgusAI/{self.current_version} updater"})

    def check(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._state["status"] in {"checking", "downloading", "installing"}:
                return dict(self._state)
            ready = ((dict(self._state), self._manifest, self._package)
                     if self._state["status"] == "downloaded" and self._manifest and self._package else None)
            if ready and not force:
                return dict(self._state)
            if not force and self._last_check is not None and time.monotonic() - self._last_check < CHECK_INTERVAL:
                return dict(self._state)
            self._state.update(status="checking", error="", error_code="")
        client = self._http()
        try:
            with client.stream("GET", MANIFEST_URL, follow_redirects=False) as response:
                response.raise_for_status()
                if response.status_code != 200:
                    raise ValueError("Update manifest did not return HTTP 200")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_MANIFEST_BYTES:
                        raise ValueError("Update manifest is too large")
                manifest = validate_manifest(json.loads(body))
            with self._lock:
                self._last_check = time.monotonic()
                newer = _version(manifest["version"]) > _version(self.current_version)
                asset = manifest["assets"].get(self.platform_key)
                reuse_ready = False
                if ready and manifest["version"] == ready[1]["version"]:
                    old_asset = ready[1]["assets"].get(self.platform_key)
                    if asset == old_asset:
                        try:
                            verify_package(ready[2], asset)
                            reuse_ready = True
                        except (OSError, ValueError):
                            pass
                self._manifest = manifest
                if reuse_ready:
                    self._package = ready[2]
                    self._save_ready()
                    return self._set(status="downloaded", version=manifest["version"], notes=manifest["notes"],
                                     release_url=manifest["release_url"], published_at=manifest["published_at"],
                                     total_bytes=asset["size"], downloaded_bytes=asset["size"], error="", error_code="")
                self._package = None
                (self.cache_dir / "verified-update.json").unlink(missing_ok=True)
                return self._set(status=("available" if asset else "unsupported") if newer else "up_to_date",
                                 version=manifest["version"], notes=manifest["notes"],
                                 release_url=manifest["release_url"], published_at=manifest["published_at"],
                                 total_bytes=asset["size"] if asset else 0, downloaded_bytes=0)
        except Exception as exc:
            # Automatic checks back off on offline startup as well as success.
            self._last_check = time.monotonic()
            if ready:
                with self._lock:
                    self._state, self._manifest, self._package = ready
                    return self._set(error=str(exc), error_code="check_failed")
            return self._set(status="error", error=str(exc))
        finally:
            if self._client is None:
                client.close()

    def start_download(self) -> dict[str, Any]:
        with self._lock:
            if self._state["status"] in {"checking", "downloading", "installing"}:
                return dict(self._state)
            if not self.packaged:
                return self._set(error="Source installations must be updated with their package manager or Git")
            if not self._manifest or _version(self._manifest["version"]) <= _version(self.current_version):
                return self._set(error="Check for a newer update first")
            asset = self._manifest["assets"].get(self.platform_key)
            if not asset:
                return self._set(status="unsupported", error="No package is available for this platform")
            if self._state["status"] == "installing":
                return dict(self._state)
            self._cancel.clear()
            self._state.update(status="downloading", downloaded_bytes=0, total_bytes=asset["size"], error="")
            self._worker = threading.Thread(target=self._download, args=(dict(asset),), daemon=True,
                                            name="argusai-update-download")
            self._worker.start()
            return dict(self._state)

    def cancel_download(self) -> dict[str, Any]:
        self._cancel.set()
        return self.status()

    def _download(self, asset: dict[str, Any]) -> None:
        temporary: Path | None = None
        client = self._http()
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self.cache_dir / asset["name"]
            if path.exists():
                try:
                    verify_package(path, asset)
                    self._package = path
                    self._save_ready()
                    self._set(status="downloaded", downloaded_bytes=asset["size"])
                    return
                except ValueError:
                    path.unlink()
            handle, filename = tempfile.mkstemp(prefix="download-", suffix=".part", dir=self.cache_dir)
            temporary = Path(filename)
            with os.fdopen(handle, "wb") as output:
                url = asset["url"]
                for _ in range(6):
                    parsed = urlsplit(url)
                    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                            or parsed.hostname not in {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}):
                        raise ValueError("Untrusted update download redirect")
                    with client.stream("GET", url, follow_redirects=False) as response:
                        if response.is_redirect:
                            url = str(response.url.join(response.headers["location"]))
                            continue
                        response.raise_for_status()
                        if response.status_code != 200:
                            raise ValueError("Update download did not return HTTP 200")
                        length = response.headers.get("content-length")
                        if length is not None and int(length) != asset["size"]:
                            raise ValueError("Update download has an unexpected content length")
                        received = 0
                        for chunk in response.iter_bytes(256 * 1024):
                            if self._cancel.is_set():
                                raise InterruptedError("Update download cancelled")
                            received += len(chunk)
                            if received > asset["size"]:
                                raise ValueError("Update download exceeds its expected size")
                            output.write(chunk)
                            self._set(downloaded_bytes=received)
                        output.flush()
                        os.fsync(output.fileno())
                        break
                else:
                    raise ValueError("Too many update download redirects")
            if self._cancel.is_set():
                raise InterruptedError("Update download cancelled")
            verify_package(temporary, asset)
            os.replace(temporary, path)
            self._package = path
            self._save_ready()
            self._set(status="downloaded", downloaded_bytes=asset["size"], error="")
        except InterruptedError:
            self._set(status="available", downloaded_bytes=0, error="")
        except Exception as exc:
            self._set(status="error", error=str(exc))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if self._client is None:
                client.close()

    def prepare_install(self) -> dict[str, Any]:
        """Stage a verified payload and return a helper command; never run it."""
        with self._lock:
            if self._state["status"] != "downloaded" or not self._manifest or not self._package:
                return {"ok": False, "error": "Download and verify an update before installing"}
            if not self.packaged:
                return {"ok": False, "error": "Source installations cannot be replaced automatically"}
            try:
                asset = self._manifest["assets"][self.platform_key]
                verify_package(self._package, asset)
                target = _target(self.executable, self.platform_key)
                for protected in self.protected_paths:
                    if protected == target or protected.is_relative_to(target):
                        raise ValueError("Move application data outside the installation directory before updating")
                if self.cache_dir == target or self.cache_dir.is_relative_to(target):
                    raise ValueError("Update cache must be outside the installation directory")
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                (self.cache_dir / "install-result.json").unlink(missing_ok=True)
                if self.platform_key.startswith("win-"):
                    command, helper = self._windows_helper(target)
                else:
                    command, helper = self._unix_helper(target)
                self._set(status="installing", error="")
                return {"ok": True, "command": command, "cwd": str(self.cache_dir),
                        "helper_path": str(helper), "package_path": str(self._package)}
            except Exception as exc:
                manual = isinstance(exc, PermissionError) or any(text in str(exc) for text in (
                    "not writable", "outside the installation", "packaged ArgusAI", "installed ArgusAI.app"))
                self._set(error=str(exc), error_code="manual_install_required" if manual else "install_prepare_failed",
                          install_mode="manual" if manual else self._state["install_mode"])
                return {"ok": False, "error": str(exc), "error_code": self._state["error_code"],
                        "package_path": str(self._package)}

    def installation_aborted(self, error: str = "") -> dict[str, Any]:
        """Restore the ready state when the GUI cannot launch/quit for update."""
        return self._set(status="downloaded" if self._package else "available", error=error)

    def _windows_helper(self, target: Path) -> tuple[list[str], Path]:
        helper = self.cache_dir / f"install-{self._manifest['version']}.ps1"
        elevated = self.cache_dir / f"install-elevated-{self._manifest['version']}.ps1"
        log = self.cache_dir / "install.log"
        report = self.cache_dir / "install-result.json"
        backup = target.with_name(target.name + ".previous-" + self._manifest["version"] + "-" + str(time.time_ns()))
        failed = target.with_name(target.name + ".failed-" + self._manifest["version"] + "-" + str(time.time_ns()))
        powershell = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        # The first helper retains the user token. Only the directory swap and
        # installer are elevated. Rollback covers files, not OS registry writes.
        elevated.write_text(f"""$ErrorActionPreference = 'Stop'
$target = {_ps_quote(target)}
$backup = {_ps_quote(backup)}
$failed = {_ps_quote(failed)}
$moved = $false
try {{
  $running = @(Get-CimInstance Win32_Process -Filter "Name='ArgusAI.exe'" | Where-Object {{ $_.ExecutablePath -and ([IO.Path]::GetFullPath($_.ExecutablePath) -ieq {_ps_quote(self.executable)}) }})
  if ($running.Count -gt 0) {{ throw 'Another ArgusAI instance is using this installation; close it before updating' }}
  $hashAlgorithm = [Security.Cryptography.SHA256]::Create()
  $packageStream = [IO.File]::OpenRead({_ps_quote(self._package)})
  try {{ $packageHash = ([BitConverter]::ToString($hashAlgorithm.ComputeHash($packageStream))).Replace('-','').ToLowerInvariant() }} finally {{ $packageStream.Dispose(); $hashAlgorithm.Dispose() }}
  if ($packageHash -cne {_ps_quote(self._manifest['assets'][self.platform_key]['sha256'])}) {{ throw 'Package changed after verification' }}
  if (Test-Path -LiteralPath $backup) {{ throw 'Update backup already exists' }}
  Move-Item -LiteralPath $target -Destination $backup
  $moved = $true
  $arguments = '/S /UPDATEPID={os.getpid()} /D=' + $target
  $installer = Start-Process -FilePath {_ps_quote(self._package)} -ArgumentList $arguments -Wait -PassThru
  if ($installer.ExitCode -ne 0) {{ throw ('Installer failed: ' + $installer.ExitCode) }}
  if (-not (Test-Path -LiteralPath {_ps_quote(self.executable)} -PathType Leaf)) {{ throw 'Installer did not create the application executable' }}
  $probeInfo = New-Object System.Diagnostics.ProcessStartInfo
  $probeInfo.FileName = {_ps_quote(self.executable)}
  $probeInfo.Arguments = 'version --json'
  $probeInfo.UseShellExecute = $false
  $probeInfo.CreateNoWindow = $true
  $probeInfo.RedirectStandardOutput = $true
  $probeInfo.RedirectStandardError = $true
  $probe = [System.Diagnostics.Process]::Start($probeInfo)
  $probeOutput = $probe.StandardOutput.ReadToEndAsync()
  $probeErrors = $probe.StandardError.ReadToEndAsync()
  if (-not $probe.WaitForExit(30000)) {{ $probe.Kill(); throw 'New application version check timed out' }}
  if ($probe.ExitCode -ne 0) {{ throw 'New application cannot run its version check' }}
  $metadata = $probeOutput.GetAwaiter().GetResult() | ConvertFrom-Json
  if ($metadata.version -cne {_ps_quote(self._manifest['version'])}) {{ throw 'New application version does not match the update manifest' }}
  @{{ok=$true; version={_ps_quote(self._manifest['version'])}; current_version={_ps_quote(self.current_version)}; backup=$backup}} | ConvertTo-Json | Set-Content -LiteralPath {_ps_quote(report)} -Encoding UTF8
  exit 0
}} catch {{
  $reason = $_.Exception.Message
  if ($moved) {{
    try {{
      if (Test-Path -LiteralPath $target) {{ Move-Item -LiteralPath $target -Destination $failed }}
      Move-Item -LiteralPath $backup -Destination $target
    }} catch {{ $reason += '; rollback failed: ' + $_.Exception.Message }}
  }}
  @{{ok=$false; version={_ps_quote(self._manifest['version'])}; current_version={_ps_quote(self.current_version)}; error=$reason; backup=$backup}} | ConvertTo-Json | Set-Content -LiteralPath {_ps_quote(report)} -Encoding UTF8
  exit 1
}}
""", encoding="utf-8-sig")
        script = f"""$ErrorActionPreference = 'Stop'
Start-Transcript -LiteralPath {_ps_quote(log)} -Append | Out-Null
try {{
  $waitDeadline = [DateTime]::UtcNow.AddMilliseconds(120000)
  foreach ($parentId in @({','.join(str(pid) for pid in _update_parent_pids())})) {{
    $parent = Get-Process -Id $parentId -ErrorAction SilentlyContinue
    if ($parent) {{
      $remaining = [int][Math]::Max(0, ($waitDeadline - [DateTime]::UtcNow).TotalMilliseconds)
      if (-not $parent.WaitForExit($remaining)) {{ throw 'Application did not exit; update cancelled' }}
    }}
  }}
  $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + {_ps_quote(elevated)} + '"'
  $installer = Start-Process -FilePath {_ps_quote(powershell)} -ArgumentList $arguments -Verb RunAs -Wait -PassThru
  if ($installer.ExitCode -ne 0) {{ throw 'Update failed; see install-result.json for details' }}
}} catch {{
  if (-not (Test-Path -LiteralPath {_ps_quote(report)})) {{ @{{ok=$false; version={_ps_quote(self._manifest['version'])}; current_version={_ps_quote(self.current_version)}; error=$_.Exception.Message}} | ConvertTo-Json | Set-Content -LiteralPath {_ps_quote(report)} -Encoding UTF8 }}
}} finally {{
  if (-not (Get-Process -Id {os.getpid()} -ErrorAction SilentlyContinue)) {{
    if (Test-Path -LiteralPath {_ps_quote(target / 'launch-gui.vbs')}) {{ Start-Process -FilePath 'wscript.exe' -ArgumentList ('"' + {_ps_quote(target / 'launch-gui.vbs')} + '"') }}
    elseif (Test-Path -LiteralPath {_ps_quote(self.executable)}) {{ Start-Process -FilePath {_ps_quote(self.executable)} -ArgumentList '--gui' -WindowStyle Hidden }}
  }}
  Stop-Transcript | Out-Null
}}
"""
        helper.write_text(script, encoding="utf-8-sig")
        return [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(helper)], helper

    def _unix_helper(self, target: Path) -> tuple[list[str], Path]:
        if not os.access(target.parent, os.W_OK) or not os.access(target, os.W_OK):
            raise ValueError("Installation directory is not writable; install the downloaded package manually")
        stage = Path(tempfile.mkdtemp(prefix=".argusai-update-", dir=target.parent))
        try:
            if self.platform_key.startswith("linux-"):
                payload = _safe_extract_linux(self._package, stage, self._manifest["version"])
            else:
                mount = stage / "mount"
                mount.mkdir()
                attached = False
                try:
                    subprocess.run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint",
                                    str(mount), str(self._package)], check=True, capture_output=True, timeout=120)
                    attached = True
                    app = mount / "ArgusAI.app"
                    if app.is_symlink() or not (app / "Contents/Info.plist").is_file():
                        raise ValueError("Update image has no ArgusAI.app")
                    subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                                   check=True, capture_output=True, timeout=120)
                    payload = stage / "ArgusAI.app"
                    subprocess.run(["/usr/bin/ditto", str(app), str(payload)], check=True, capture_output=True, timeout=120)
                finally:
                    if attached:
                        subprocess.run(["/usr/bin/hdiutil", "detach", str(mount)], check=True, capture_output=True, timeout=30)
            launcher = _macos_launcher(payload) if self.platform_key.startswith("macos-") else payload / "ArgusAI"
            # Preflight another onefile binary as a fresh process, not a child
            # of the currently running application's bootstrap.
            probe_env = dict(os.environ)
            for key in ("NUITKA_ONEFILE_PARENT", "NUITKA_ONEFILE_BINARY", "NUITKA_RESUME_FILENAME"):
                probe_env.pop(key, None)
            probe = subprocess.run([str(launcher), "version", "--json"], capture_output=True, text=True,
                                   check=True, timeout=30, env=probe_env)
            if json.loads(probe.stdout).get("version") != self._manifest["version"]:
                raise ValueError("New application version does not match the update manifest")
            helper = self.cache_dir / f"install-{self._manifest['version']}.sh"
            backup = target.with_name(target.name + ".previous-" + self._manifest["version"])
            if backup.exists():
                raise ValueError(f"An earlier update backup exists: {backup}; inspect it before retrying")
            q = shlex.quote
            wait_condition = " || ".join(f"kill -0 {pid} 2>/dev/null" for pid in _update_parent_pids())
            launch = f"/usr/bin/open {q(str(target))}" if self.platform_key.startswith("macos-") else f"{q(str(target / 'ArgusAI'))} --gui"
            if self.platform_key.startswith("macos-"):
                instance_check = f"""if [ ! -x /usr/sbin/lsof ]; then printf '%s\\n' '{{"ok":false,"error":"Cannot check other running instances"}}' > "$report"; exit 1; fi
others=$(/usr/sbin/lsof -t -- {q(str(self.executable))} 2>/dev/null || true)
if [ -n "$others" ]; then printf '%s\\n' '{{"ok":false,"error":"Another application instance is running"}}' > "$report"; exit 1; fi"""
            else:
                instance_check = """for process in /proc/[0-9]*/exe; do
  [ -e "$process" ] || continue
  running=$(readlink "$process" 2>/dev/null || true)
  if [ "$running" = "$target/ArgusAI" ]; then printf '%s\\n' '{"ok":false,"error":"Another application instance is running"}' > "$report"; exit 1; fi
done"""
            script = f"""#!/bin/sh
set -eu
unset NUITKA_ONEFILE_PARENT NUITKA_ONEFILE_BINARY NUITKA_RESUME_FILENAME
exec >>{q(str(self.cache_dir / 'install.log'))} 2>&1
target={q(str(target))}
payload={q(str(payload))}
backup={q(str(backup))}
stage={q(str(stage))}
report={q(str(self.cache_dir / 'install-result.json'))}
count=0
while {wait_condition}; do
  count=$((count+1))
  if [ "$count" -ge 120 ]; then printf '%s\\n' '{{"ok":false,"error":"Application did not exit"}}' > "$report"; exit 1; fi
  sleep 1
done
{instance_check}
rollback() {{
  if [ -d "$backup" ] && [ ! -e "$target" ]; then mv "$backup" "$target"; fi
  printf '%s\\n' '{{"ok":false,"error":"Update replacement failed; previous application retained"}}' > "$report"
}}
trap rollback HUP INT TERM
if ! mv "$target" "$backup"; then rollback; exit 1; fi
if ! mv "$payload" "$target"; then rollback; exit 1; fi
printf '%s\\n' '{{"ok":true}}' > "$report"
# Keep the prior installation for recovery. User data is never copied/deleted.
{launch} >/dev/null 2>&1 &
trap - HUP INT TERM
"""
            result_prefix = (f'{{"version":"{self._manifest["version"]}",'
                             f'"current_version":"{self.current_version}","ok":')
            helper.write_text(script.replace('{"ok":', result_prefix), encoding="utf-8", newline="\n")
            helper.chmod(0o700)
            return ["/bin/sh", str(helper)], helper
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
