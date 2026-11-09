"""Maintain the one vendor tool ArgusAI still ships in-tree.

Python, Node.js, webcrack, restringer, and WABT are managed on demand under the
user's ``data_root/tools/runtimes`` and must never be written into this source
tree. Only innoextract remains here, because it is needed to unpack installers
before any managed toolchain exists.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import os
import platform
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parent.parent
VENDOR = REPO / "argusai" / "vendor"
INNOEXTRACT_VERSION = "1.9"
# Published by upstream at:
# https://constexpr.org/innoextract/files/innoextract-1.9/SHA256SUMS
INNOEXTRACT_ARCHIVE_SHA256 = {
    "innoextract-1.9-windows.zip": "6989342c9b026a00a72a38f23b62a8e6a22cc5de69805cf47d68ac2fec993065",
    "innoextract-1.9-linux.tar.xz": "008efe5011476ccc4aae17c3e22038b5a1bc5c7aad2b9d4d869537bf3874d21f",
}
_DOWNLOAD_RETRIES = 3
_OFFICIAL_FILES_BASE = "https://constexpr.org/innoextract/files"
_GITHUB_RELEASE_BASE = "https://github.com/dscharrer/innoextract/releases/download"


def _platform_subdir() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    os_label = "win" if system.startswith("win") else ("macos" if system == "darwin" else "linux")
    arch_label = "x64" if machine in {"x86_64", "amd64"} else ("arm64" if machine in {"arm64", "aarch64"} else machine)
    return f"{os_label}-{arch_label}"


def _innoextract_archive_spec(version: str) -> tuple[tuple[str, ...], str, str] | None:
    plat = _platform_subdir()
    if plat == "win-x64":
        filename = f"innoextract-{version}-windows.zip"
    elif plat == "linux-x64":
        filename = f"innoextract-{version}-linux.tar.xz"
    else:
        return None
    expected_sha256 = INNOEXTRACT_ARCHIVE_SHA256.get(filename)
    if not expected_sha256:
        raise RuntimeError(f"No pinned SHA-256 for innoextract archive: {filename}")
    urls = (
        f"{_OFFICIAL_FILES_BASE}/innoextract-{version}/{filename}",
        f"{_GITHUB_RELEASE_BASE}/{version}/{filename}",
    )
    return urls, filename, expected_sha256


class ArchiveChecksumMismatch(RuntimeError):
    """Downloaded vendor bytes do not match the source-pinned archive."""


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _retryable_download_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 429} or exc.code >= 500
    return isinstance(
        exc,
        (
            urllib.error.URLError,
            TimeoutError,
            ConnectionError,
            http.client.IncompleteRead,
        ),
    )


def _source_fallback_allowed(exc: BaseException) -> bool:
    return _retryable_download_error(exc) or (
        isinstance(exc, urllib.error.HTTPError) and exc.code == 404
    )


def _download(
    urls: Sequence[str],
    destination: Path,
    *,
    label: str,
    expected_sha256: str,
    opener: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """Download a pinned vendor archive with bounded retries and atomic commit."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    fetch = opener or urllib.request.urlopen
    failures: list[str] = []
    for url in urls:
        for attempt in range(1, _DOWNLOAD_RETRIES + 1):
            partial.unlink(missing_ok=True)
            print(f"  [{label}] GET {url} (attempt {attempt}/{_DOWNLOAD_RETRIES})")
            try:
                request = urllib.request.Request(
                    url,
                    headers={"User-Agent": "ArgusAI-vendor-runtime/1"},
                )
                with fetch(request, timeout=120) as response, partial.open("wb") as handle:
                    while chunk := response.read(256 * 1024):
                        handle.write(chunk)
                actual_sha256 = _sha256(partial)
                if actual_sha256.lower() != expected_sha256.lower():
                    raise ArchiveChecksumMismatch(
                        f"SHA-256 mismatch from {url}: expected {expected_sha256}, got {actual_sha256}"
                    )
                os.replace(partial, destination)
                print(f"  [{label}] verified SHA-256 {actual_sha256}")
                return url
            except ArchiveChecksumMismatch:
                partial.unlink(missing_ok=True)
                raise
            except Exception as exc:
                partial.unlink(missing_ok=True)
                failures.append(f"{url} attempt {attempt}: {type(exc).__name__}: {exc}")
                print(f"  [{label}] download failed: {exc}")
                if _retryable_download_error(exc) and attempt < _DOWNLOAD_RETRIES:
                    sleeper(float(2 ** (attempt - 1)))
                    continue
                if _source_fallback_allowed(exc):
                    break
                raise
    raise RuntimeError(f"all {label} download sources failed: {'; '.join(failures)}")


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
    urls: Sequence[str],
    filename: str,
    expected_sha256: str,
    destination: Path,
    label: str,
    force: bool,
    flatten_single_dir: bool = True,
) -> None:
    archive = VENDOR / "_downloads" / filename
    if archive.exists() and _sha256(archive).lower() != expected_sha256.lower():
        print(f"  [{label}] cached archive checksum mismatch; downloading a clean copy")
        archive.unlink()
    if force or not archive.exists():
        _download(
            urls,
            archive,
            label=label,
            expected_sha256=expected_sha256,
        )
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
    archive_spec = _innoextract_archive_spec(INNOEXTRACT_VERSION)
    if archive_spec is None:
        print(f"  [innoextract] no portable archive for {plat}; use the managed reverse-tool installer.")
        return
    destination = VENDOR / "innoextract" / plat
    executable_name = "innoextract.exe" if os.name == "nt" else "innoextract"
    if not force and destination.is_dir() and any(destination.rglob(executable_name)):
        print(f"  [innoextract] already available at {destination}")
        return
    urls, filename, expected_sha256 = archive_spec
    _replace_from_archive(
        urls=urls,
        filename=filename,
        expected_sha256=expected_sha256,
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
