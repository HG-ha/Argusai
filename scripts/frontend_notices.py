#!/usr/bin/env python3
"""Collect frontend runtime dependency notices offline from the installed lockfile.

The output deliberately includes the complete non-dev dependency set, even when
Vite removes some modules. It is a notice inventory, not a license compatibility
opinion. Missing documents fail except for explicitly reviewed, version-pinned
metadata-only entries, which remain visible warnings in both API and CLI usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import warnings
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
FALLBACK_MANIFEST = ROOT / "scripts/packaging/frontend-licenses.json"
OUTPUT_NAME = "THIRD_PARTY_NOTICES.txt"
DOCUMENT = re.compile(r"^(licen[cs]e|copying|notice|copyright)(?:[._-].*)?$", re.I)
DOCUMENT_DIR = re.compile(r"^(licen[cs]es?|copying|notices?|copyrights?)$", re.I)
LICENSE_DOCUMENT = re.compile(r"^(licen[cs]e|copying)(?:[._-].*)?$", re.I)
PACKAGE_PART = re.compile(r"[A-Za-z0-9_~.-]+\Z")


class NoticeReviewWarning(UserWarning):
    """A pinned exception still needs upstream license text confirmation."""


def _read_json(path: Path) -> dict:
    data = json.loads(path.read_bytes().decode("utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return data


def _inside(path: Path, boundary: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(boundary):
        raise ValueError(f"Path escapes {boundary}: {path}")
    return resolved


def _package_name(key: str) -> str:
    """Accept only npm's nested node_modules/package lockfile paths."""
    if not isinstance(key, str) or "\\" in key:
        raise ValueError(f"Invalid package lock path: {key!r}")
    parts = key.split("/")
    if PurePosixPath(key).is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Invalid package lock path: {key!r}")
    position, name = 0, ""
    while position < len(parts):
        if parts[position] != "node_modules":
            raise ValueError(f"Invalid package lock path: {key!r}")
        position += 1
        if position == len(parts):
            raise ValueError(f"Invalid package lock path: {key!r}")
        first = parts[position]
        if first.startswith("@"):
            if not PACKAGE_PART.fullmatch(first[1:]) or position + 1 == len(parts):
                raise ValueError(f"Invalid package lock path: {key!r}")
            second = parts[position + 1]
            if not PACKAGE_PART.fullmatch(second):
                raise ValueError(f"Invalid package lock path: {key!r}")
            name = f"{first}/{second}"
            position += 2
        else:
            if not PACKAGE_PART.fullmatch(first):
                raise ValueError(f"Invalid package lock path: {key!r}")
            name = first
            position += 1
    return name


def _documents(package: Path, node_modules: Path) -> list[tuple[str, bytes, bool]]:
    documents = []
    for child in sorted(package.iterdir(), key=lambda item: item.name):
        if child.is_file() and DOCUMENT.fullmatch(child.name):
            _inside(child, package)
            candidates = [child]
        elif child.is_dir() and DOCUMENT_DIR.fullmatch(child.name):
            _inside(child, package)
            candidates = sorted(child.rglob("*"))
        else:
            continue
        for path in candidates:
            _inside(path, package)
            _inside(path, node_modules)
            if not path.is_file():
                continue
            raw = path.read_bytes()
            if not raw.strip():
                raise ValueError(f"Empty license/notice document: {path}")
            # Do not silently replace undecodable original notice characters.
            raw.decode("utf-8")
            is_license = bool(LICENSE_DOCUMENT.fullmatch(child.name)) or bool(
                DOCUMENT_DIR.fullmatch(child.name) and child.name.lower().startswith(("licen", "copying"))
            )
            documents.append((path.relative_to(package).as_posix(), raw, is_license))
    return documents


def _fallback_document(record: dict, manifest_dir: Path, field: str) -> tuple[str, bytes]:
    filename = record[field]
    path = _inside(manifest_dir / filename, manifest_dir)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record[f"{field}_sha256"]:
        raise ValueError(f"Fallback license checksum mismatch: {path}")
    if not raw.strip():
        raise ValueError(f"Empty fallback license: {path}")
    raw.decode("utf-8")
    return filename, raw


def _render(webui: Path) -> bytes:
    webui = webui.resolve()
    lock_path = _inside(webui / "package-lock.json", webui)
    lock_bytes = lock_path.read_bytes()
    lock = _read_json(lock_path)
    if lock.get("lockfileVersion") not in {2, 3} or not isinstance(lock.get("packages"), dict):
        raise ValueError("Expected package-lock.json lockfileVersion 2 or 3 with packages")
    node_modules = _inside(webui / "node_modules", webui)
    if not node_modules.is_dir():
        raise ValueError("Missing frontend node_modules; run npm ci before collecting notices")
    manifest = _read_json(FALLBACK_MANIFEST)
    fallbacks = manifest["packages"]
    output = [
        "ArgusAI frontend third-party notices\n",
        f"package-lock.json SHA256: {hashlib.sha256(lock_bytes).hexdigest()}\n",
        "Scope: installed non-dev lockfile dependencies, including their runtime closure.\n",
        "Third-party components retain their own licenses. This inventory does not change them.\n",
        "Metadata-only exceptions are incomplete upstream notices, not a completed compliance review.\n",
    ]
    # Validate *all* paths before interpreting the dev marker.
    names = {key: _package_name(key) for key in lock["packages"] if key}
    for key in sorted(names):
        entry = lock["packages"][key]
        if not isinstance(entry, dict):
            raise ValueError(f"Invalid lock entry: {key}")
        if entry.get("dev") is True:
            continue
        if entry.get("link"):
            raise ValueError(f"Linked frontend packages need an explicit notice policy: {key}")
        package = _inside(webui / key, node_modules)
        metadata = _read_json(_inside(package / "package.json", package))
        name, version = entry.get("name", names[key]), entry.get("version")
        if not version or metadata.get("name") != name or metadata.get("version") != version:
            raise ValueError(f"Installed package name/version does not match lock: {key}")
        documents = _documents(package, node_modules)
        output += [
            f"\n{'=' * 78}\n{name}@{version}\nInstalled path: {key}\n",
            f"Locked source: {json.dumps(entry.get('resolved'), ensure_ascii=False)}\n",
            f"Repository: {json.dumps(metadata.get('repository'), ensure_ascii=False, sort_keys=True)}\n",
            f"Declared license metadata: {json.dumps(metadata.get('license'), ensure_ascii=False, sort_keys=True)}\n",
        ]
        if not any(is_license for _, _, is_license in documents):
            record = fallbacks.get(name)
            if record is None or record.get("version") != version:
                raise ValueError(f"Missing original license text for {name}@{version}; no version-matched fallback")
            output.append(f"Pinned upstream metadata source: {record['metadata_source']}\n")
            if record["kind"] == "upstream-license":
                filename, raw = _fallback_document(record, FALLBACK_MANIFEST.parent, "file")
                output.append(f"Upstream original license source: {record['source']}\n")
                documents.append((f"checked upstream fallback/{filename}", raw, True))
            elif record["kind"] == "metadata-only":
                actual = {field: metadata.get(field) for field in record["metadata"]}
                if actual != record["metadata"]:
                    raise ValueError(f"Metadata-only exception no longer matches {name}@{version}")
                message = f"{name}@{version}: UPSTREAM LICENSE TEXT MISSING; metadata-only exception requires upstream confirmation"
                warnings.warn(message, NoticeReviewWarning, stacklevel=2)
                output += [
                    f"WARNING: {message}\n",
                    f"Original package metadata: {json.dumps(actual, ensure_ascii=False, sort_keys=True)}\n",
                    f"Review limitation: {record['limitation']}\n",
                    "The author field is not a substitute for a copyright notice.\n",
                    "The following SPDX template is a separate reference, NOT an upstream license document.\n",
                    "Template placeholders are intentionally unfilled; no copyright owner or year is inferred.\n",
                    f"Reference template source: {record['reference_source']}\n",
                ]
                filename, raw = _fallback_document(record, FALLBACK_MANIFEST.parent, "reference_file")
                documents.append((f"REFERENCE ONLY; upstream original missing/{filename}", raw, False))
            else:
                raise ValueError(f"Unknown fallback kind for {name}@{version}")
        for filename, raw, _ in documents:
            output += [
                f"\n--- BEGIN {filename} (SHA256 {hashlib.sha256(raw).hexdigest()}) ---\n",
                raw.decode("utf-8"),
                f"\n--- END {filename} ---\n",
            ]
    return "".join(output).encode("utf-8")


def _output_path(webui: Path) -> Path:
    webui = webui.resolve()
    dist = webui / "dist"
    output = dist / OUTPUT_NAME
    if dist.is_symlink() or output.is_symlink():
        raise ValueError("Frontend notice output must not be a symbolic link")
    _inside(output, dist)
    _inside(dist, webui)
    return output


def generate_notices(webui: Path) -> Path:
    """Write deterministic notices; never fetch packages or license documents."""
    content = _render(webui)
    output = _output_path(webui)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(content)
    return output


def check_notices(webui: Path) -> None:
    """Reject missing/stale notices, lock changes, or installed dependency drift."""
    expected = _render(webui)
    output = _output_path(webui)
    if not output.is_file():
        raise ValueError(f"Missing frontend notices: {output}; generate them before packaging")
    if output.read_bytes() != expected:
        raise ValueError("Frontend notices are stale or modified; regenerate after npm ci")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--webui", type=Path, default=ROOT / "argusai/webui")
    parser.add_argument("--check", action="store_true", help="Validate existing notices without writing")
    args = parser.parse_args(argv)
    try:
        if args.check:
            check_notices(args.webui)
            print("Frontend notices match the lockfile and installed dependencies")
        else:
            print(generate_notices(args.webui))
    except (OSError, ValueError, KeyError) as exc:
        print(f"Frontend notices error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
