#!/usr/bin/env python3
"""Bump the ArgusAI version from a single command.

The single source of truth is ``argusai/__init__.py`` (``__version__``):

* ``pyproject.toml`` reads it dynamically via hatchling (``[tool.hatch.version]``).
* The desktop GUI shows it at runtime via ``Api.app_info()`` → ``argusai.__version__``.

So the version only ever has to change in ``__init__.py``. This script does
that and also syncs the dev-only webui package metadata (``package.json`` /
``package-lock.json``), which npm owns and which never ships in the wheel.

Usage:
    python scripts/bump_version.py 0.1.0
    python scripts/bump_version.py            # just print the current version
    python scripts/bump_version.py --check --tag v0.1.0
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "argusai" / "__init__.py"
PACKAGE_JSON = ROOT / "argusai" / "webui" / "package.json"
PACKAGE_LOCK = ROOT / "argusai" / "webui" / "package-lock.json"

_SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")


def current_version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', INIT.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit(f"could not find __version__ in {INIT}")
    return m.group(1)


def _sub_once(path: Path, pattern: str, repl: str, *, count: int = 1) -> int:
    text = path.read_bytes().decode("utf-8")
    new, n = re.subn(pattern, repl, text, count=count)
    if n:
        path.write_bytes(new.encode("utf-8"))
    return n


def set_version(version: str) -> None:
    if not _SEMVER.match(version):
        raise SystemExit(f"'{version}' is not a semantic version (e.g. 0.1.0)")
    old = current_version()
    n = _sub_once(INIT, r'__version__\s*=\s*"[^"]*"', f'__version__ = "{version}"')
    if n != 1:
        raise SystemExit(f"expected exactly one __version__ in {INIT}, replaced {n}")
    # webui metadata: package.json has one top-level "version"; package-lock has
    # the package's own version at the root and under packages[""] (first two).
    if PACKAGE_JSON.exists():
        _sub_once(PACKAGE_JSON, r'("version":\s*)"[^"]*"', rf'\g<1>"{version}"')
    if PACKAGE_LOCK.exists():
        _sub_once(PACKAGE_LOCK, r'("version":\s*)"[^"]*"', rf'\g<1>"{version}"', count=2)
    print(f"version: {old} -> {version}")
    print("  argusai/__init__.py (source of truth; pyproject + GUI derive it)")
    print("  argusai/webui/package.json, package-lock.json (synced)")
    print("remember to rebuild the webui (npm run build) so the About page reflects it")


def check_version(tag: str | None = None) -> str:
    """Validate release metadata without importing the application or writing files."""
    expected = current_version()
    if not _SEMVER.fullmatch(expected):
        raise SystemExit(f"Invalid Python package version: {expected}")
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    versions = {
        "package.json": package.get("version"),
        "package-lock.json": lock.get("version"),
        'package-lock.json packages[""]': lock.get("packages", {}).get("", {}).get("version"),
    }
    for location, found in versions.items():
        if found != expected:
            raise SystemExit(f"Version mismatch: {location} has {found!r}, expected {expected!r}")
    if tag is not None and tag.removeprefix("v") != expected:
        raise SystemExit(f"Release tag {tag!r} does not match version {expected!r}; use v{expected}")
    return expected


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?")
    parser.add_argument("--check", action="store_true", help="Validate all package versions without changing files")
    parser.add_argument("--tag", help="Also require a matching release tag, e.g. v0.1.0")
    args = parser.parse_args(argv[1:])
    if args.version and (args.check or args.tag is not None):
        parser.error("Choose a version update or --check/--tag, not both")
    if args.check or args.tag is not None:
        print(f"Version metadata verified: {check_version(args.tag)}")
    elif args.version is None:
        print(current_version())
    else:
        set_version(args.version.strip().removeprefix("v"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
