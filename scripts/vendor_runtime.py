"""Maintain the one vendor tool ArgusAI still ships in-tree.

Python, Node.js, webcrack, restringer, and WABT are managed on demand under the
user's ``data_root/tools/runtimes`` and must never be written into this source
tree. Only innoextract remains here, because it is needed to unpack installers
before any managed toolchain exists.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
VENDOR = REPO / "argusai" / "vendor"
INNOEXTRACT_VERSION = "1.9"


def _platform_subdir() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_label = "win" if system.startswith("win") else ("macos" if system == "darwin" else "linux")
    arch_label = "x64" if machine in {"x86_64", "amd64"} else ("arm64" if machine in {"arm64", "aarch64"} else machine)
    return f"{os_label}-{arch_label}"


def _innoextract_archive_url(version: str) -> tuple[str, str] | None:
    plat = _platform_subdir()
    if plat == "win-x64":
        filename = f"innoextract-{version}-windows.zip"
    elif plat == "linux-x64":
        filename = f"innoextract-{version}-linux.tar.xz"
    else:
        return None
    return f"https://github.com/dscharrer/innoextract/releases/download/{version}/{filename}", filename


def _download(url: str, destination: Path, *, label: str) -> None:
    print(f"  [{label}] GET {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as handle:
            while chunk := response.read(256 * 1024):
                handle.write(chunk)
    except urllib.error.URLError as exc:
        print(f"  [{label}] download failed: {exc}")
        raise


def _extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for item in bundle.infolist():
                target = (root / item.filename).resolve()
                if target != root and root not in target.parents:
                    raise RuntimeError(f"archive path traversal blocked: {item.filename}")
            bundle.extractall(destination)
    elif archive.name.endswith(".tar.xz"):
        with tarfile.open(archive, "r:xz") as bundle:
            bundle.extractall(destination, filter="data")
    elif archive.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(destination, filter="data")
    else:
        raise RuntimeError(f"Unknown archive type: {archive}")


def _replace_from_archive(
    *,
    url: str,
    filename: str,
    destination: Path,
    label: str,
    force: bool,
    flatten_single_dir: bool = True,
) -> None:
    archive = VENDOR / "_downloads" / filename
    if force or not archive.exists():
        _download(url, archive, label=label)
    staging = VENDOR / "_downloads" / f"_extract_{filename}"
    shutil.rmtree(staging, ignore_errors=True)
    _extract(archive, staging)
    entries = list(staging.iterdir())
    payload = entries[0] if flatten_single_dir and len(entries) == 1 and entries[0].is_dir() else staging
    shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(payload), str(destination))


def install_innoextract(*, force: bool = False) -> None:
    plat = _platform_subdir()
    url_spec = _innoextract_archive_url(INNOEXTRACT_VERSION)
    if url_spec is None:
        print(f"  [innoextract] no portable archive for {plat}; use the managed reverse-tool installer.")
        return
    destination = VENDOR / "innoextract" / plat
    executable_name = "innoextract.exe" if os.name == "nt" else "innoextract"
    if not force and destination.is_dir() and any(destination.rglob(executable_name)):
        print(f"  [innoextract] already available at {destination}")
        return
    url, filename = url_spec
    _replace_from_archive(
        url=url,
        filename=filename,
        destination=destination,
        label="innoextract",
        force=force,
    )
    matches = list(destination.rglob(executable_name))
    if not matches:
        raise RuntimeError(f"innoextract archive did not contain {executable_name}")
    if os.name != "nt":
        for executable in matches:
            executable.chmod(executable.stat().st_mode | 0o111)
    print(f"  [innoextract] installed v{INNOEXTRACT_VERSION} at {destination}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Re-download and replace existing assets.")
    parser.add_argument("--skip-innoextract", action="store_true", help="Skip innoextract.")
    args = parser.parse_args(argv)

    print("ArgusAI retained vendor-tool installer")
    print(f"Platform: {_platform_subdir()}")
    if not args.skip_innoextract:
        install_innoextract(force=args.force)
    if not args.force:
        shutil.rmtree(VENDOR / "_downloads", ignore_errors=True)
    print("Done. Python, Node, and WABT toolchains are installed on demand from Settings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
