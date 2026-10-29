#!/usr/bin/env python3
"""Offline original-document inventory for installed, uv.lock-pinned packages.

This is a conservative environment inventory, not a statement that every build
or development dependency ships, nor a complete audit of native static libraries.
Only the standard library is imported. No packages are installed or imported.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import platform
import re
import sys
import tomllib
import warnings
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "uv.lock"
FALLBACK_MANIFEST = ROOT / "scripts/packaging/python-licenses.json"
OUTPUT_NAME = "THIRD_PARTY_NOTICES.txt"
DOCUMENT = re.compile(r"(?:^|[._-])(?:licen[cs]es?|copying|notices?|copyright|thirdpartynotices)(?:$|[._-])", re.I)
LICENSE = re.compile(r"(?:^|[._-])(?:licen[cs]es?|copying)(?:$|[._-])", re.I)
DOCUMENT_DIR = re.compile(r"^(?:licen[cs]es?|copying|notices?)$", re.I)


class NoticeReviewWarning(UserWarning):
    """A reviewed dependency still lacks original or nested license coverage."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _name(value: str) -> str:
    result = re.sub(r"[-_.]+", "-", value).lower()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", result):
        raise ValueError(f"Unsafe distribution name: {value!r}")
    return result


def _relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError(f"Unsafe document path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise ValueError(f"Unsafe document path: {value!r}")
    return path


def _inside(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes source/output directory: {path}")
    return resolved


def _roles(packages: list[dict]) -> dict[str, list[str]]:
    by_name: dict[str, list[dict]] = {}
    for entry in packages:
        by_name.setdefault(_name(entry["name"]), []).append(entry)
    own = next((p for p in packages if p["name"] == "argusai"), {})
    groups = {"runtime": own.get("dependencies", [])}
    groups.update(own.get("dev-dependencies", {}))
    roles: dict[str, set[str]] = {}
    for role, roots in groups.items():
        pending = [_name(item["name"]) for item in roots]
        seen: set[str] = set()
        while pending:
            name = pending.pop()
            if name in seen:
                continue
            seen.add(name)
            roles.setdefault(name, set()).add(role)
            for item in by_name.get(name, []):
                pending.extend(_name(d["name"]) for d in item.get("dependencies", []))
    return {name: sorted(values) for name, values in roles.items()}


def _original(path: Path) -> bytes:
    raw = path.read_bytes()
    if not raw.strip():
        raise ValueError(f"Empty original document: {path}")
    return raw


def _fallback(record: dict) -> tuple[str, bytes]:
    relative = str(_relative(record["file"]))
    path = _inside(FALLBACK_MANIFEST.parent / relative, FALLBACK_MANIFEST.parent)
    raw = _original(path)
    if _sha(raw) != record["sha256"]:
        raise ValueError(f"Fallback checksum mismatch: {path}")
    return relative, raw


def _python_license() -> Path | None:
    base = Path(sys.base_prefix)
    candidates = [base / "LICENSE.txt", base / "LICENSE_PYTHON.txt", base / "LICENSE", base / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "LICENSE.txt"]
    return next((p for p in candidates if p.is_file()), None)


def _collect() -> tuple[dict[str, bytes], dict]:
    lock_raw = LOCK_PATH.read_bytes()
    lock = tomllib.loads(lock_raw.decode("utf-8"))
    manifest_raw = FALLBACK_MANIFEST.read_bytes()
    fallbacks = json.loads(manifest_raw)
    packages = lock["package"]
    locked: dict[str, list[dict]] = {}
    for entry in packages:
        if "version" in entry:
            locked.setdefault(_name(entry["name"]), []).append(entry)
    roles = _roles(packages)
    documents: dict[str, bytes] = {}
    inventory: list[dict] = []
    messages: list[str] = []
    installed = sorted(metadata.distributions(), key=lambda d: _name(d.metadata["Name"]))
    seen: set[str] = set()
    for dist in installed:
        name = _name(dist.metadata["Name"])
        if name not in locked or name == "argusai":
            continue
        if name in seen:
            raise ValueError(f"Duplicate installed distribution: {name}")
        seen.add(name)
        version = dist.version
        if not re.fullmatch(r"[A-Za-z0-9.!+_-]+", version):
            raise ValueError(f"Unsafe distribution version: {version!r}")
        entry = next((p for p in locked[name] if p["version"] == version), None)
        if entry is None:
            raise ValueError(f"Installed version does not match uv.lock: {name}@{version}")
        record = {
            "name": name, "version": version, "roles": roles.get(name, ["lock-inventory"]),
            "locked_source": entry.get("source"), "locked_sdist": entry.get("sdist"),
            "declared_license": dist.metadata.get("License-Expression") or dist.metadata.get("License"),
            "project_urls": dist.metadata.get_all("Project-URL") or [],
            "home_page": dist.metadata.get("Home-page"), "documents": [], "warnings": [],
        }
        base = Path(dist.locate_file("")).resolve()
        dist_files = list(dist.files or [])
        has_license = False

        def add(relative: str, raw: bytes, source: str) -> None:
            target = f"packages/{name}/{version}/{relative}"
            _relative(target)
            if target in documents and documents[target] != raw:
                raise ValueError(f"Conflicting document: {target}")
            documents[target] = raw
            record["documents"].append({"path": target, "sha256": _sha(raw), "source": source})

        for item in sorted(dist_files, key=str):
            raw_path = str(item)
            # Recognize a would-be document even when its path uses an unsafe
            # Windows separator; _relative then rejects rather than normalizes it.
            parts = PurePosixPath(raw_path.replace("\\", "/")).parts
            if not (DOCUMENT.search(parts[-1]) or any(DOCUMENT_DIR.fullmatch(p) for p in parts[:-1])):
                continue
            relative = str(_relative(raw_path))
            source = _inside(Path(dist.locate_file(item)), base)
            if not source.is_file():
                raise ValueError(f"Installed original document is missing: {name}: {relative}")
            raw = _original(source)
            add(relative, raw, f"installed distribution RECORD: {relative}")
            has_license |= bool(LICENSE.search(parts[-1]) or any(p.lower().startswith(("licen", "copying")) for p in parts[:-1] if DOCUMENT_DIR.fullmatch(p)))
        if not has_license:
            fallback = fallbacks.get("packages", {}).get(name)
            if not fallback or fallback["version"] != version:
                raise ValueError(f"Missing original license text for {name}@{version}; no version-matched reviewed fallback")
            if fallback["kind"] == "upstream-license":
                filename, raw = _fallback(fallback)
                add(f"reviewed-upstream/{filename}", raw, fallback["source"])
                record["reviewed_fallback"] = fallback
            elif fallback["kind"] == "missing-original":
                record["reviewed_fallback"] = fallback
                record["warnings"].append(f"{name}@{version}: ORIGINAL LICENSE TEXT MISSING. {fallback['limitation']}")
            else:
                raise ValueError(f"Unknown fallback kind: {name}")

        for supplement in fallbacks.get("binary_supplements", []):
            if supplement["package"] != name:
                continue
            matched = []
            for relative, expected in supplement["binaries"].items():
                _relative(relative)
                candidate = _inside(base / relative, base)
                if not candidate.is_file():
                    continue
                if _sha(candidate.read_bytes()) == expected:
                    matched.append({"path": relative, "sha256": expected})
                else:
                    record["warnings"].append(f"Unreviewed binary SHA256: {relative}; pinned supplemental notices were NOT applied to this binary")
            if matched:
                for original in supplement["documents"]:
                    filename, raw = _fallback(original)
                    add(f"reviewed-binary/{filename}", raw, supplement["source"])
                record.setdefault("binary_supplements", []).append({"component": supplement["component"], "version": supplement["version"], "source": supplement["source"], "matched_binaries": matched})
        messages.extend(record["warnings"])
        inventory.append(record)

    python = {"implementation": platform.python_implementation(), "version": platform.python_version(), "platform": sys.platform}
    original = _python_license()
    if original is not None:
        raw = _original(original)
        target = f"python/{python['implementation']}-{python['version']}/LICENSE.txt"
        documents[target] = raw
        python.update({"document": target, "sha256": _sha(raw), "source": "Current interpreter installation / " + original.name})
    else:
        messages.append("Current interpreter LICENSE.txt was not found; CPython/platform runtime original notice coverage remains incomplete")
    report = {
        "format": 1, "scope": "Installed uv.lock dependencies; conservative runtime/build/dev inventory, NOT final binary composition or native static-library clearance",
        "lock_sha256": _sha(lock_raw), "fallback_manifest_sha256": _sha(manifest_raw),
        "role_policy": "Dependency reachability is conservative across platform markers; build/dev inventory does not imply shipping. A compiler's GPL does not by itself license its output; inspect its runtime exception and actual copied content.",
        "python": python, "packages": inventory, "warnings": messages,
    }
    lines = ["ArgusAI Python dependency original notices\n", f"uv.lock SHA256: {_sha(lock_raw)}\n", report["scope"] + "\n", report["role_policy"] + "\n", "Third parties retain their own licenses. Metadata is a declaration, not replacement license text.\n", "Complete original bytes are preserved in the referenced files. Native libraries, downloaded runtimes and final bundle composition require separate review.\n"]
    for record in inventory:
        lines.extend([f"\n{record['name']}@{record['version']} | roles: {', '.join(record['roles'])}\n", f"Locked source: {json.dumps(record['locked_source'], ensure_ascii=False)}\n", f"Declared license metadata: {json.dumps(record['declared_license'], ensure_ascii=False)}\n"])
        for item in record["documents"]:
            lines.append(f"  {item['path']} | SHA256 {item['sha256']} | source: {item['source']}\n")
    lines.append(f"\nInterpreter: {json.dumps(python, ensure_ascii=False)}\n")
    lines.extend(f"WARNING: {message}\n" for message in messages)
    documents[OUTPUT_NAME] = "".join(lines).encode("utf-8")
    documents["manifest.json"] = _json(report)
    return documents, report


def _output(output: Path) -> Path:
    output = Path(output)
    if ".." in output.parts or output.is_symlink() or any(p.is_symlink() for p in output.parents):
        raise ValueError(f"Unsafe output directory: {output}")
    resolved = output.resolve()
    if resolved == ROOT or ROOT.is_relative_to(resolved):
        raise ValueError(f"Output must be a dedicated directory: {output}")
    if output.exists():
        if not output.is_dir():
            raise ValueError(f"Output is not a directory: {output}")
        for child in output.rglob("*"):
            if child.is_symlink():
                raise ValueError(f"Unsafe output symlink: {child}")
            _inside(child, resolved)
    return resolved


def generate_notices(output: Path) -> Path:
    """Write a dedicated notice directory; return its readable index."""
    output = _output(output)
    files, report = _collect()
    existing = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()} if output.exists() else set()
    if existing and not (output / "manifest.json").is_file():
        raise ValueError(f"Refusing to overwrite an unowned nonempty notice directory: {output}")
    if existing - files.keys():
        raise ValueError(f"Stale extra notice files; use a fresh output directory: {sorted(existing - files.keys())}")
    for relative, raw in files.items():
        path = _inside(output / str(_relative(relative)), output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    for message in report["warnings"]:
        warnings.warn(message, NoticeReviewWarning, stacklevel=2)
    return output / OUTPUT_NAME


def check_notices(output: Path) -> None:
    """Read-only verification against current lock, installation and originals."""
    output = _output(output)
    expected, report = _collect()
    actual = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()} if output.exists() else set()
    if actual != expected.keys() or any((output / name).read_bytes() != raw for name, raw in expected.items()):
        raise ValueError("Python notices are missing, stale or modified; regenerate into a fresh dedicated directory")
    for message in report["warnings"]:
        warnings.warn(message, NoticeReviewWarning, stacklevel=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "build/python-notices")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            check_notices(args.output)
        else:
            print(generate_notices(args.output))
    except (OSError, ValueError, KeyError) as error:
        print(f"Python notices: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
