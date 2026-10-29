"""Managed reverse-toolchain installs under ``data_root/tools/reverse/``.

Installs are persistent (not session sandbox). Successful installs write
``settings.toolchain.paths.<tool>`` so ``toolchain.py`` resolves them on
every subsequent launch.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from argusai.core.infra.win_stdio import quiet_subprocess_kwargs
from argusai.core.managed_runtimes import python_executable, runtime_status as managed_runtime_status
from argusai.core.paths import PathManager
from argusai.core.reverse.toolchain import clear_toolchain_cache, detect_tool
from argusai.core.reverse.toolchain_settings import SETTINGS_PROBE_ALIASES, SETTINGS_TOOLCHAIN_KEYS

log = logging.getLogger("argusai.toolchain_install")

_USER_AGENT = "ArgusAI-toolchain-install/1.0"
_PIP_PACKAGES: dict[str, tuple[str, ...]] = {
    "frida": ("frida-tools",),
    "objection": ("objection",),
    # Mandiant capa CLI is published as ``flare-capa`` (console script: capa).
    # The PyPI name ``capa`` is an unrelated French library and must not be used.
    "capa": ("flare-capa",),
}


@dataclass(frozen=True)
class InstallSpec:
    tool: str
    method: str  # pip | jadx_zip | apktool_jar | adb_zip | build_tools_zip | yara_tar | manual
    docs_url: str = ""
    notes: str = ""


_INSTALL_SPECS: dict[str, InstallSpec] = {
    "frida": InstallSpec("frida", "pip", docs_url="https://frida.re/docs/installation/", notes="Managed pip venv under data_root/tools/reverse/venv"),
    "objection": InstallSpec("objection", "pip", docs_url="https://github.com/sensepost/objection", notes="Same managed venv as frida"),
    "capa": InstallSpec(
        "capa",
        "pip",
        docs_url="https://github.com/mandiant/capa",
        notes="Managed pip: flare-capa (provides capa CLI)",
    ),
    "jadx": InstallSpec("jadx", "jadx_zip", docs_url="https://github.com/skylot/jadx/releases"),
    "apktool": InstallSpec("apktool", "apktool_jar", docs_url="https://apktool.org/docs/install", notes="Requires Java on PATH"),
    "adb": InstallSpec("adb", "adb_zip", docs_url="https://developer.android.com/tools/releases/platform-tools"),
    "zipalign": InstallSpec("zipalign", "build_tools_zip", docs_url="https://developer.android.com/tools/releases/build-tools"),
    "apksigner": InstallSpec("apksigner", "build_tools_zip", docs_url="https://developer.android.com/tools/releases/build-tools"),
    "yara": InstallSpec(
        "yara",
        "yara_tar",
        docs_url="https://github.com/VirusTotal/yara/releases",
        notes="Official GitHub releases ship Windows win64 zips only; macOS/Linux need a manual build/package",
    ),
    "ghidra": InstallSpec("ghidra", "ghidra_zip", docs_url="https://github.com/NationalSecurityAgency/ghidra/releases", notes="High-quality decompiler backend for binary_decompile. Large download (~hundreds of MB) and REQUIRES a JDK (21+) on PATH."),
    "innoextract": InstallSpec("innoextract", "innoextract_zip", docs_url="https://github.com/dscharrer/innoextract/releases", notes="Inno Setup installer unpacker for installer_unpack (Windows zip / Linux tarball; macOS via 'brew install innoextract')."),
    "ida": InstallSpec(
        "ida",
        "manual",
        docs_url="https://hex-rays.com/ida-pro/",
        notes=(
            "Commercial, licence-bound — cannot be downloaded by argusai. Install IDA on the "
            "host, then point settings.toolchain.paths.ida at the headless binary "
            "(idat64/idat, idat64.exe/idat.exe on Windows) so batch analysis works without a GUI."
        ),
    ),
    "ffmpeg": InstallSpec(
        "ffmpeg",
        "manual",
        docs_url="https://ffmpeg.org/download.html",
        notes=(
            "Optional. Only used to turn attached videos into keyframes for models that "
            "accept stills but not video; without it those attachments degrade to a text "
            "note. Install via the OS package manager (winget/brew/apt) or point "
            "settings.toolchain.paths.ffmpeg at the binary — ffprobe is picked up beside it."
        ),
    ),
    "jarsigner": InstallSpec("jarsigner", "manual", docs_url="https://adoptium.net/", notes="Ships with JDK — install Temurin/OpenJDK"),
    "class-dump": InstallSpec("class-dump", "manual", docs_url="http://stevenyg.github.io/2016/04/unofficial-class-dump-for-iOS/", notes="macOS host tool only"),
}


def installable_tools() -> tuple[str, ...]:
    """Tool keys for which reverse_toolchain_install can perform a managed download.

    Stable enum source for the AI tool schema, the GUI button gating, and the
    settings.toolchain.paths key set. Manual-only entries (jarsigner,
    class-dump) are excluded — those still point at docs_url only.
    """
    return tuple(
        sorted(
            {
                key
                for key, spec in _INSTALL_SPECS.items()
                if spec.method not in {"manual"}
            }
        )
    )


def managed_reverse_root(data_root: str | Path | None = None) -> Path:
    root = Path(data_root) if data_root else PathManager().tools_dir
    return (root / "reverse").resolve()


def managed_venv_dir(data_root: str | Path | None = None) -> Path:
    return managed_reverse_root(data_root) / "venv"


def install_spec(tool: str) -> InstallSpec | None:
    key = str(tool or "").strip().lower()
    return _INSTALL_SPECS.get(key)


def install_inventory(*, data_root: str | Path | None = None) -> dict[str, Any]:
    items = []
    for key in SETTINGS_TOOLCHAIN_KEYS:
        spec = _INSTALL_SPECS.get(key)
        items.append({
            "tool": key,
            "can_install": bool(spec and spec.method not in {"manual"}),
            "method": spec.method if spec else "",
            "docs_url": spec.docs_url if spec else "",
            "notes": spec.notes if spec else "",
        })
    return {"ok": True, "root": str(managed_reverse_root(data_root)), "tools": items}


def enrich_toolchain_scan_result(
    scan: dict[str, Any],
    *,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Attach managed-install metadata to ``scan_toolchain`` / settings scan rows."""
    tools = dict(scan.get("tools") or {})
    enriched: dict[str, dict[str, Any]] = {}
    for key, info in tools.items():
        spec = install_spec(key)
        row = dict(info)
        row["can_install"] = bool(spec and spec.method not in {"manual"})
        row["docs_url"] = spec.docs_url if spec else ""
        row["install_notes"] = spec.notes if spec else ""
        enriched[key] = row
    scan["tools"] = enriched
    scan["install_root"] = str(managed_reverse_root(data_root))
    return scan


def _platform_os() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _http_get_json(url: str, *, timeout: int = 30) -> Any:
    req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _download(url: str, dest: Path, *, timeout: int = 120) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(req, timeout=timeout) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh, length=1024 * 256)


def _run(argv: list[str], *, cwd: Path | None = None, timeout: int = 600) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **quiet_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc), "argv": argv}
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "")[:8000],
        "stderr": (proc.stderr or "")[:8000],
        "argv": argv,
    }


def _venv_python(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _venv_executable(venv: Path, name: str) -> Path:
    if sys.platform == "win32":
        exe = venv / "Scripts" / f"{name}.exe"
        if exe.is_file():
            return exe
        return venv / "Scripts" / name
    return venv / "bin" / name


def _ensure_venv(data_root: str | Path | None) -> Path:
    venv = managed_venv_dir(data_root)
    if not (venv / "pyvenv.cfg").is_file():
        status = managed_runtime_status("python", data_root)
        if not status.get("healthy"):
            raise RuntimeError(
                "managed_runtime_required: install or repair the Python sandbox "
                "environment before installing pip-based reverse tools"
            )
        base_python = python_executable(data_root)
        venv.parent.mkdir(parents=True, exist_ok=True)
        run = _run([str(base_python), "-m", "venv", str(venv)], timeout=120)
        if not run["ok"]:
            raise RuntimeError(run.get("stderr") or run.get("error") or "venv creation failed")
    return venv


def _install_pip_tool(tool: str, packages: tuple[str, ...], *, data_root: str | Path | None) -> Path:
    venv = _ensure_venv(data_root)
    py = _venv_python(venv)
    run = _run([str(py), "-m", "pip", "install", "--upgrade", *packages], timeout=900)
    if not run["ok"]:
        raise RuntimeError((run.get("stderr") or run.get("stdout") or "pip install failed").strip())
    exe = _venv_executable(venv, tool if tool != "frida" else "frida")
    if not exe.is_file():
        raise RuntimeError(f"{tool} executable not found after pip install: {exe}")
    return exe.resolve()


def _install_jadx(*, data_root: str | Path | None) -> Path:
    release = _http_get_json("https://api.github.com/repos/skylot/jadx/releases/latest")
    assets = release.get("assets") or []
    asset = next((a for a in assets if str(a.get("name", "")).startswith("jadx-") and str(a.get("name", "")).endswith(".zip")), None)
    if not asset:
        raise RuntimeError("jadx release zip not found on GitHub")
    url = str(asset.get("browser_download_url") or "")
    dest_root = managed_reverse_root(data_root) / "jadx"
    if dest_root.exists():
        shutil.rmtree(dest_root, ignore_errors=True)
    zip_path = dest_root.parent / "downloads" / str(asset.get("name") or "jadx.zip")
    _download(url, zip_path)
    dest_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_root)
    # jadx zip layout: bin/jadx(.bat)
    for candidate in (
        dest_root / "bin" / ("jadx.bat" if sys.platform == "win32" else "jadx"),
        dest_root / "bin" / "jadx",
        dest_root / "jadx.bat",
        dest_root / "jadx",
    ):
        if candidate.is_file():
            if sys.platform != "win32":
                candidate.chmod(candidate.stat().st_mode | 0o111)
            return candidate.resolve()
    raise RuntimeError("jadx binary not found after extract")


def _install_apktool(*, data_root: str | Path | None) -> Path:
    if not shutil.which("java"):
        raise RuntimeError("Java (java) not found on PATH — required for APKTool")
    release = _http_get_json("https://api.github.com/repos/iBotPeaches/Apktool/releases/latest")
    tag = str(release.get("tag_name") or "").lstrip("v")
    assets = release.get("assets") or []
    jar_asset = next((a for a in assets if str(a.get("name", "")).endswith(".jar")), None)
    if not jar_asset:
        raise RuntimeError("apktool jar not found on GitHub releases")
    root = managed_reverse_root(data_root) / "apktool"
    root.mkdir(parents=True, exist_ok=True)
    jar_path = root / str(jar_asset.get("name") or "apktool.jar")
    _download(str(jar_asset.get("browser_download_url")), jar_path)
    if sys.platform == "win32":
        wrapper = root / "apktool.bat"
        wrapper.write_text(
            f'@echo off\r\njava -jar "{jar_path}" %*\r\n',
            encoding="utf-8",
        )
        return wrapper.resolve()
    wrapper = root / "apktool"
    wrapper.write_text(
        f'#!/bin/sh\nexec java -jar "{jar_path}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | 0o111)
    return wrapper.resolve()


def _android_platform_tools_zip_url(os_name: str | None = None) -> str:
    """Google platform-tools archive URL (macOS tag is ``darwin``, not ``macos``)."""
    resolved = os_name or _platform_os()
    tag = {"windows": "windows", "macos": "darwin", "linux": "linux"}[resolved]
    return f"https://dl.google.com/android/repository/platform-tools-latest-{tag}.zip"


def _install_adb(*, data_root: str | Path | None) -> Path:
    os_name = _platform_os()
    tag = {"windows": "windows", "macos": "darwin", "linux": "linux"}[os_name]
    url = _android_platform_tools_zip_url(os_name)
    root = managed_reverse_root(data_root) / "platform-tools"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    zip_path = managed_reverse_root(data_root) / "downloads" / f"platform-tools-{tag}.zip"
    try:
        _download(url, zip_path)
    except URLError as exc:
        raise RuntimeError(f"failed to download Android platform-tools from {url}: {exc}") from exc
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(root.parent)
    adb = root / ("adb.exe" if sys.platform == "win32" else "adb")
    if not adb.is_file():
        raise RuntimeError(f"adb not found after extract: {adb}")
    if sys.platform != "win32":
        adb.chmod(adb.stat().st_mode | 0o111)
    return adb.resolve()


def _android_build_tools_zip_url(os_name: str | None = None) -> str:
    """Return the Google Android SDK archive URL for build-tools 34.0.0.

    Filenames use ``build-tools_r34-{os}.zip`` (see sdkmanager /
    ``repository2-3.xml``). The mistaken ``r34.0.0`` form 404s.
    """
    resolved = os_name or _platform_os()
    tag = {"windows": "windows", "macos": "macosx", "linux": "linux"}[resolved]
    return f"https://dl.google.com/android/repository/build-tools_r34-{tag}.zip"


def _install_build_tools(*, data_root: str | Path | None) -> dict[str, Path]:
    os_name = _platform_os()
    tag = {"windows": "windows", "macos": "macosx", "linux": "linux"}[os_name]
    url = _android_build_tools_zip_url(os_name)
    root = managed_reverse_root(data_root) / "build-tools"
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    zip_path = managed_reverse_root(data_root) / "downloads" / f"build-tools-{tag}.zip"
    try:
        _download(url, zip_path, timeout=300)
    except URLError as exc:
        raise RuntimeError(
            f"failed to download Android build-tools (apksigner/zipalign) from {url}: {exc}"
        ) from exc
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(root.parent)
    # android-14/zipalign, apksigner
    android_dir = next(root.parent.glob("android-*"), None)
    if android_dir is None:
        android_dir = root
    out: dict[str, Path] = {}
    for name in ("zipalign", "apksigner"):
        cand = android_dir / (f"{name}.exe" if sys.platform == "win32" else name)
        if sys.platform == "win32" and not cand.is_file() and name == "apksigner":
            cand = android_dir / "apksigner.bat"
        if cand.is_file():
            if sys.platform != "win32":
                cand.chmod(cand.stat().st_mode | 0o111)
            out[name] = cand.resolve()
    if not out:
        raise RuntimeError("build-tools zip did not contain zipalign/apksigner")
    return out


def _yara_asset_pattern(os_name: str | None = None) -> re.Pattern[str]:
    resolved = os_name or _platform_os()
    patterns = {
        "windows": r"yara-.*-win64\.zip",
        "macos": r"yara-.*-mac\.(tar\.gz|zip)",
        "linux": r"yara-.*-linux\.(tar\.gz|zip)",
    }
    return re.compile(patterns[resolved], re.I)


def _pick_yara_release_asset(os_name: str | None = None) -> dict[str, Any]:
    """Pick a prebuilt YARA archive.

    Recent ``/releases/latest`` tags often ship no binaries (source-only).
    Walk the release list until a matching OS asset appears. Upstream currently
    publishes Windows win64 zips only.
    """
    resolved = os_name or _platform_os()
    pat = _yara_asset_pattern(resolved)
    releases = _http_get_json(
        "https://api.github.com/repos/VirusTotal/yara/releases?per_page=30"
    )
    if not isinstance(releases, list):
        raise RuntimeError("unexpected yara releases payload from GitHub")
    for release in releases:
        if not isinstance(release, dict):
            continue
        assets = release.get("assets") or []
        asset = next((a for a in assets if pat.search(str(a.get("name", "")))), None)
        if asset:
            return asset
    raise RuntimeError(
        f"yara prebuilt asset not found for {resolved} "
        "(VirusTotal releases currently ship Windows win64 zips only; "
        "install via package manager on macOS/Linux, then set the path)"
    )


def _install_yara(*, data_root: str | Path | None) -> Path:
    asset = _pick_yara_release_asset()
    name = str(asset.get("name") or "yara.bin")
    url = str(asset.get("browser_download_url") or "")
    dl_dir = managed_reverse_root(data_root) / "downloads"
    archive = dl_dir / name
    _download(url, archive)
    extract_root = managed_reverse_root(data_root) / "yara"
    if extract_root.exists():
        shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True, exist_ok=True)
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(extract_root)
    elif name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as tf:
            tf.extractall(extract_root)
    else:
        raise RuntimeError(f"unsupported yara archive: {name}")
    for path in extract_root.rglob("yara*"):
        if path.is_file() and path.name.lower().startswith("yara") and not path.suffix == ".txt":
            if sys.platform == "win32" and path.suffix.lower() != ".exe":
                continue
            if sys.platform != "win32":
                path.chmod(path.stat().st_mode | 0o111)
            return path.resolve()
    raise RuntimeError("yara binary not found after extract")


def _install_innoextract(*, data_root: str | Path | None) -> Path:
    os_name = _platform_os()
    if os_name == "macos":
        raise RuntimeError(
            "innoextract auto-install is not supported on macOS — install via "
            "'brew install innoextract' and set settings.toolchain.paths.innoextract."
        )
    release = _http_get_json("https://api.github.com/repos/dscharrer/innoextract/releases/latest")
    assets = release.get("assets") or []
    patterns = {
        "windows": re.compile(r"innoextract-.*-windows\.zip$", re.I),
        "linux": re.compile(r"innoextract-.*-linux\.tar\.xz$", re.I),
    }
    pat = patterns.get(os_name)
    asset = next((a for a in assets if pat and pat.search(str(a.get("name", "")))), None)
    if not asset:
        raise RuntimeError(f"innoextract release asset not found for {os_name}")
    name = str(asset.get("name") or "innoextract.bin")
    url = str(asset.get("browser_download_url") or "")
    archive = managed_reverse_root(data_root) / "downloads" / name
    _download(url, archive, timeout=300)
    extract_root = managed_reverse_root(data_root) / "innoextract"
    if extract_root.exists():
        shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True, exist_ok=True)
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive, "r") as zf:
            zf.extractall(extract_root)
    elif name.endswith(".tar.xz"):
        with tarfile.open(archive, "r:xz") as tf:
            tf.extractall(extract_root)
    else:
        raise RuntimeError(f"unsupported innoextract archive: {name}")
    exe_name = "innoextract.exe" if sys.platform == "win32" else "innoextract"
    for path in extract_root.rglob(exe_name):
        if path.is_file():
            if sys.platform != "win32":
                path.chmod(path.stat().st_mode | 0o111)
            return path.resolve()
    raise RuntimeError("innoextract executable not found after extract")


def _install_ghidra(*, data_root: str | Path | None) -> Path:
    if not shutil.which("java"):
        raise RuntimeError("Java (JDK 21+) not found on PATH — required to run Ghidra headless")
    release = _http_get_json("https://api.github.com/repos/NationalSecurityAgency/ghidra/releases/latest")
    assets = release.get("assets") or []
    pat = re.compile(r"ghidra_.*_PUBLIC_.*\.zip$", re.I)
    asset = next((a for a in assets if pat.search(str(a.get("name", "")))), None)
    if not asset:
        raise RuntimeError("ghidra release zip not found on GitHub")
    name = str(asset.get("name") or "ghidra.zip")
    url = str(asset.get("browser_download_url") or "")
    archive = managed_reverse_root(data_root) / "downloads" / name
    _download(url, archive, timeout=900)
    extract_root = managed_reverse_root(data_root) / "ghidra"
    if extract_root.exists():
        shutil.rmtree(extract_root, ignore_errors=True)
    extract_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(extract_root)
    headless_name = "analyzeHeadless.bat" if sys.platform == "win32" else "analyzeHeadless"
    for path in extract_root.rglob(headless_name):
        if path.is_file():
            if sys.platform != "win32":
                # zipfile drops exec bits — restore them for the launch scripts.
                support = path.parent
                for script in (path, support / "launch.sh"):
                    if script.is_file():
                        script.chmod(script.stat().st_mode | 0o111)
            return path.resolve()
    raise RuntimeError("analyzeHeadless not found after extract")


def persist_toolchain_paths(settings_mgr: Any, paths: dict[str, str]) -> None:
    from dataclasses import replace

    def _update(current):
        merged = dict(current.toolchain_paths or {})
        for key, value in paths.items():
            k = str(key or "").strip().lower()
            v = str(value or "").strip()
            if k and v:
                merged[k] = v
        return replace(current, toolchain_paths=merged)

    update = getattr(settings_mgr, "update", None)
    if callable(update):
        update(_update)
    else:
        settings_mgr.save(_update(settings_mgr.get()))


def install_reverse_tool(
    tool: str,
    *,
    settings_mgr: Any,
    data_root: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Install one reverse CLI into data_root/tools/reverse and persist settings path.

    ``force=False`` (default): if the tool is already present (settings path
    or PATH probe succeeds), return a short ``already_installed`` result
    without re-downloading. ``force=True`` always re-downloads and overwrites.
    """
    key = str(tool or "").strip().lower()
    spec = install_spec(key)
    if spec is None:
        return {"ok": False, "error": f"unknown tool: {tool}"}
    if spec.method == "manual":
        return {
            "ok": False,
            "blocked": True,
            "error": f"{key} cannot be auto-installed by argusai",
            "docs_url": spec.docs_url,
            "suggested_fix": f"Install {key} manually. See {spec.docs_url}",
        }

    if not force:
        existing = detect_tool(key)
        if existing.get("available"):
            return {
                "ok": True,
                "tool": key,
                "path": str(existing.get("path") or ""),
                "already_installed": True,
                "verified": True,
                "toolchain": existing,
                "install_root": str(managed_reverse_root(data_root)),
                "message": f"{key} already installed at {existing.get('path')}. Pass force=true to re-download.",
            }

    started = time.time()
    try:
        paths_to_save: dict[str, str] = {}
        installed_path = ""

        if spec.method == "pip":
            packages = _PIP_PACKAGES.get(key)
            if not packages:
                return {"ok": False, "error": f"no pip package mapped for {key}"}
            exe = _install_pip_tool(key, packages, data_root=data_root)
            installed_path = str(exe)
            paths_to_save[key] = installed_path

        elif spec.method == "jadx_zip":
            exe = _install_jadx(data_root=data_root)
            installed_path = str(exe)
            paths_to_save["jadx"] = installed_path

        elif spec.method == "apktool_jar":
            exe = _install_apktool(data_root=data_root)
            installed_path = str(exe)
            paths_to_save["apktool"] = installed_path

        elif spec.method == "adb_zip":
            exe = _install_adb(data_root=data_root)
            installed_path = str(exe)
            paths_to_save["adb"] = installed_path

        elif spec.method == "build_tools_zip":
            pair = _install_build_tools(data_root=data_root)
            paths_to_save.update({k: str(v) for k, v in pair.items()})
            installed_path = str(pair.get(key) or next(iter(pair.values())))

        elif spec.method == "yara_tar":
            exe = _install_yara(data_root=data_root)
            installed_path = str(exe)
            paths_to_save["yara"] = installed_path

        elif spec.method == "innoextract_zip":
            exe = _install_innoextract(data_root=data_root)
            installed_path = str(exe)
            paths_to_save["innoextract"] = installed_path

        elif spec.method == "ghidra_zip":
            exe = _install_ghidra(data_root=data_root)
            installed_path = str(exe)
            paths_to_save["ghidra"] = installed_path

        else:
            return {"ok": False, "error": f"unsupported install method: {spec.method}"}

        persist_toolchain_paths(settings_mgr, paths_to_save)
        clear_toolchain_cache()
        probe = detect_tool(key)
        duration_ms = int((time.time() - started) * 1000)
        return {
            "ok": True,
            "tool": key,
            "path": installed_path,
            "paths_saved": paths_to_save,
            "install_root": str(managed_reverse_root(data_root)),
            "duration_ms": duration_ms,
            "verified": bool(probe.get("available")),
            "toolchain": probe,
            "message": f"Installed {key} under {managed_reverse_root(data_root)} and saved to settings.",
        }
    except (URLError, OSError, RuntimeError, zipfile.BadZipFile, tarfile.TarError) as exc:
        log.warning("toolchain install failed tool=%s err=%s", key, exc)
        return {
            "ok": False,
            "tool": key,
            "error": str(exc),
            "install_root": str(managed_reverse_root(data_root)),
            "docs_url": spec.docs_url,
        }
