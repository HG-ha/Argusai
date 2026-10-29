"""Explicit data_root relocation: preview, copy/move/switch, path rewrite.

Changing ``data_root`` must never silently orphan tasks/sessions/KB. Callers
pick one of:

- ``copy`` — duplicate managed subtrees into the new root, keep the old ones
- ``move`` — relocate managed subtrees, then point settings at the new root
- ``switch`` — only update settings (user already copied, or intentionally
  starts fresh); still rewrite absolute paths that pointed under the old root

``settings.json`` stays under the app root and is never moved. ``logs/`` lives
under ``data_root`` but is not a managed subtree: the process writing it holds
the rotating files open, so history stays put and only subsequent records
follow the new root. Python ``__pycache__`` / ``*.pyc`` under
``tools/runtimes`` is the same class of file: the running interpreter may
lock it on Windows, so migration skips those and lets the new root recreate
them.
"""

from __future__ import annotations

import errno
import hashlib
import configparser
import os
import re
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable

from argusai.core.paths import (
    PERSISTENT_DATA_SUBDIRS,
    SUBDIR_LOGS,
    SUBDIR_TOOLS,
    is_explicit_absolute,
)

MIGRATE_MODES = frozenset({"copy", "move", "switch"})

# Compatibility alias for callers/tests that imported the old local name. The
# authoritative list lives in paths.py so directory creation and migration
# cannot drift apart.
MANAGED_SUBDIRS = PERSISTENT_DATA_SUBDIRS
ProgressCallback = Callable[[dict[str, Any]], None]
_PYTHON_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
# ERROR_ACCESS_DENIED / SHARING_VIOLATION / LOCK_VIOLATION on Windows.
_WIN_IN_USE_ERRORS = frozenset({5, 32, 33})
# ERROR_INVALID_PARAMETER / INVALID_NAME / FILENAME_EXCED_RANGE.
# shutil.copy2 -> copystat -> os.utime often raises EINVAL (22) on npm tarball
# files whose mtime Windows will not accept.
_WIN_UNUSABLE_COPY_ERRORS = frozenset({87, 123, 161, 206})
_NODE_SKIP_DIRS = frozenset({
    "test",
    "tests",
    "__tests__",
    "docs",
    "doc",
    "example",
    "examples",
    ".cache",
})


def _normalize_rel(rel: str) -> str:
    return str(rel or "").replace("\\", "/").strip("/")


def _strip_long_path(path: str) -> str:
    text = str(path or "")
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def _fs_copy_path(path: str | Path) -> str:
    """Return a Windows long-path-prefixed string so copytree survives node_modules."""
    text = os.fspath(path)
    if os.name != "nt":
        return text
    abs_path = os.path.abspath(_strip_long_path(text))
    if abs_path.startswith("\\\\?\\"):
        return abs_path
    if abs_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_path[2:]
    return "\\\\?\\" + abs_path


def _is_ephemeral_name(name: str) -> bool:
    lowered = str(name or "").lower()
    return name == "__pycache__" or lowered.endswith(".pyc") or lowered.endswith(".pyo")


def _is_ephemeral_rel(rel: str) -> bool:
    return any(_is_ephemeral_name(part) for part in Path(_normalize_rel(rel)).parts)


def _under_node_modules(path: str | Path) -> bool:
    return "node_modules" in Path(_strip_long_path(os.fspath(path))).parts


def _is_node_skip_rel(rel: str) -> bool:
    parts = [part.lower() for part in Path(_normalize_rel(rel)).parts]
    if "node_modules" not in parts:
        return False
    start = parts.index("node_modules")
    return any(part in _NODE_SKIP_DIRS for part in parts[start + 1:])


def _is_skippable_rel(rel: str) -> bool:
    return _is_ephemeral_rel(rel) or _is_node_skip_rel(rel)


def _ignore_ephemeral(directory: str, names: list[str]) -> list[str]:
    ignored = [name for name in names if _is_ephemeral_name(name)]
    if _under_node_modules(directory):
        ignored.extend(
            name
            for name in names
            if name.lower() in _NODE_SKIP_DIRS and name not in ignored
        )
    return ignored


def _is_locked_oserror(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if not isinstance(exc, OSError):
        return False
    winerror = int(getattr(exc, "winerror", 0) or 0)
    if winerror in _WIN_IN_USE_ERRORS:
        return True
    return exc.errno in {errno.EACCES, errno.EPERM, errno.EBUSY}


def _is_unusable_copy_oserror(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    winerror = int(getattr(exc, "winerror", 0) or 0)
    if winerror in _WIN_UNUSABLE_COPY_ERRORS:
        return True
    nametoolong = getattr(errno, "ENAMETOOLONG", None)
    codes = {errno.EINVAL}
    if nametoolong is not None:
        codes.add(nametoolong)
    return exc.errno in codes


def _is_skippable_copy_oserror(exc: BaseException) -> bool:
    return _is_locked_oserror(exc) or _is_unusable_copy_oserror(exc)


def _emit_progress(progress: ProgressCallback | None, **payload: Any) -> None:
    if progress is None:
        return
    try:
        progress(payload)
    except Exception:
        # Progress reporting must never make a verified migration fail.
        pass


def _portable_windows_venv_launcher(command: Any) -> tuple[str, list[str]] | None:
    """Convert a venv console-script EXE into a relocation-safe Python call."""
    text = str(command or "").strip()
    if not text or not is_explicit_absolute(text):
        return None
    executable = Path(text).expanduser()
    scripts_dir = executable.parent
    if (
        executable.suffix.lower() != ".exe"
        or scripts_dir.name.lower() != "scripts"
        or scripts_dir.parent.name.lower() not in {"venv", ".venv"}
    ):
        return None
    python = scripts_dir / "python.exe"
    site_packages = scripts_dir.parent / "Lib" / "site-packages"
    if not python.is_file() or not site_packages.is_dir():
        return None

    script_name = executable.stem.lower()
    for entry_points in site_packages.glob("*.dist-info/entry_points.txt"):
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(entry_points, encoding="utf-8")
        except (OSError, configparser.Error):
            continue
        if not parser.has_section("console_scripts"):
            continue
        match = next(
            (
                (name, target)
                for name, target in parser.items("console_scripts")
                if str(name).strip().lower() == script_name
            ),
            None,
        )
        if match is None:
            continue
        target = str(match[1]).split("[", 1)[0].strip()
        module, separator, attribute = target.partition(":")
        if (
            not separator
            or not _PYTHON_NAME_RE.fullmatch(module)
            or not _PYTHON_NAME_RE.fullmatch(attribute)
        ):
            continue
        access = ".".join(["module", *attribute.split(".")])
        code = (
            f"import importlib,sys;"
            f"module=importlib.import_module({module!r});"
            f"sys.exit({access}())"
        )
        return str(python.resolve()), ["-c", code]
    return None


def repair_mcp_venv_launchers(settings: Any) -> tuple[list[dict[str, str]], Any]:
    """Rewrite moved Windows venv launchers to portable interpreter calls."""
    servers = list(getattr(settings, "mcp_servers", None) or [])
    repaired: list[dict[str, str]] = []
    updated_servers: list[dict[str, Any]] = []
    for server in servers:
        if not isinstance(server, dict):
            continue
        item = dict(server)
        portable = _portable_windows_venv_launcher(item.get("command"))
        if portable is not None:
            python, prefix_args = portable
            original = str(item.get("command") or "")
            item["command"] = python
            item["args"] = [*prefix_args, *list(item.get("args") or [])]
            repaired.append({
                "server_id": str(item.get("id") or item.get("name") or "mcp"),
                "from": original,
                "to": python,
            })
        updated_servers.append(item)
    if not repaired:
        return [], settings
    return repaired, replace(settings, mcp_servers=updated_servers)


def _abs(path: Path | str) -> Path:
    return Path(path).expanduser().absolute()


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(_abs(a))) == os.path.normcase(str(_abs(b)))


def _is_under(child: Path, parent: Path) -> bool:
    try:
        _abs(child).relative_to(_abs(parent))
        return True
    except ValueError:
        return False


def _dir_has_entries(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
        return True
    except StopIteration:
        return False
    except OSError:
        return True


def _tree_size_bytes(root: Path) -> int:
    if not root.exists():
        return 0
    total = 0
    if root.is_file():
        try:
            return int(root.stat().st_size)
        except OSError:
            return 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += int((Path(dirpath) / name).stat().st_size)
            except OSError:
                continue
    return total


def _free_bytes(path: Path) -> int | None:
    probe = _abs(path)
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    try:
        return int(shutil.disk_usage(str(probe)).free)
    except OSError:
        return None


def rebase_path(value: Any, old_root: Path, new_root: Path) -> str | None:
    """If ``value`` is an absolute path under ``old_root``, return it under ``new_root``."""
    text = str(value or "").strip()
    if not text or not is_explicit_absolute(text):
        return None
    try:
        abs_candidate = _abs(text)
        rel = abs_candidate.relative_to(_abs(old_root))
    except (ValueError, OSError):
        return None
    return str(_abs(new_root) / rel)


def _validated_rebase(value: Any, old_root: Path, new_root: Path) -> tuple[str | None, str]:
    """Return a rebased path only when its destination already exists."""
    rebased = rebase_path(value, old_root, new_root)
    if not rebased:
        return None, ""
    if Path(rebased).exists():
        return rebased, ""
    return None, rebased


def collect_path_rewrites(
    *,
    old_root: Path,
    new_root: Path,
    toolchain_paths: dict[str, str] | None,
    mcp_servers: Iterable[dict[str, Any]] | None,
    agent_runtimes: Iterable[dict[str, Any]] | None = None,
    log_dir: str = "",
    allow_managed_transfer: bool = False,
) -> dict[str, Any]:
    """List settings fields that would be rebased from old_root → new_root."""
    hits: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []

    def _collect(field: str, raw: Any) -> None:
        rebased = rebase_path(raw, old_root, new_root)
        if not rebased or rebased == str(raw or ""):
            return
        item = {"field": field, "from": str(raw), "to": rebased}
        source = Path(str(raw)).expanduser()
        transfers_with_managed_data = False
        if allow_managed_transfer and source.exists():
            try:
                rel = _abs(source).relative_to(_abs(old_root))
                transfers_with_managed_data = bool(rel.parts and rel.parts[0] in PERSISTENT_DATA_SUBDIRS)
            except ValueError:
                transfers_with_managed_data = False
        if Path(rebased).exists() or transfers_with_managed_data:
            hits.append(item)
        else:
            missing.append(item)

    for key, raw in (toolchain_paths or {}).items():
        _collect(f"toolchain.paths.{key}", raw)
    for server in mcp_servers or []:
        if not isinstance(server, dict):
            continue
        sid = str(server.get("id") or server.get("name") or "mcp")
        for field in ("command", "cwd"):
            _collect(f"mcp.{sid}.{field}", server.get(field))
        args = server.get("args") if isinstance(server.get("args"), list) else []
        for index, arg in enumerate(args):
            _collect(f"mcp.{sid}.args[{index}]", arg)
    for runtime in agent_runtimes or []:
        if not isinstance(runtime, dict):
            continue
        runtime_id = str(runtime.get("id") or "agent-runtime")
        for field in ("command", "cwd"):
            _collect(f"agent_runtimes.{runtime_id}.{field}", runtime.get(field))
        args = runtime.get("args") if isinstance(runtime.get("args"), list) else []
        for index, arg in enumerate(args):
            _collect(f"agent_runtimes.{runtime_id}.args[{index}]", arg)
    _collect("logging.log_dir", log_dir)
    return {
        "count": len(hits),
        "items": hits[:40],
        "missing_destination_count": len(missing),
        "missing_destination_items": missing[:40],
    }


def apply_path_rewrites(
    settings: Any,
    old_root: Path,
    new_root: Path,
    *,
    skipped: list[dict[str, str]] | None = None,
) -> tuple[list[dict[str, str]], Any]:
    """Return (applied hits, updated AppSettings) with path-bearing fields rebased."""
    applied: list[dict[str, str]] = []
    skipped_rewrites = skipped if skipped is not None else []

    def _candidate(field: str, raw: Any) -> str | None:
        rebased, missing = _validated_rebase(raw, old_root, new_root)
        if missing:
            skipped_rewrites.append({"field": field, "from": str(raw), "to": missing})
        return rebased

    toolchain = dict(getattr(settings, "toolchain_paths", None) or {})
    new_toolchain = dict(toolchain)
    for key, raw in list(toolchain.items()):
        field = f"toolchain.paths.{key}"
        rebased = _candidate(field, raw)
        if rebased and rebased != str(raw):
            new_toolchain[key] = rebased
            applied.append({"field": field, "from": str(raw), "to": rebased})

    servers = list(getattr(settings, "mcp_servers", None) or [])
    new_servers: list[dict[str, Any]] = []
    for server in servers:
        if not isinstance(server, dict):
            continue
        item = dict(server)
        sid = str(item.get("id") or item.get("name") or "mcp")
        for field in ("command", "cwd"):
            field_name = f"mcp.{sid}.{field}"
            rebased = _candidate(field_name, item.get(field))
            if rebased and rebased != str(item.get(field) or ""):
                applied.append({"field": field_name, "from": str(item.get(field)), "to": rebased})
                item[field] = rebased
        args = item.get("args") if isinstance(item.get("args"), list) else []
        new_args: list[str] = []
        changed_args = False
        for index, arg in enumerate(args):
            field_name = f"mcp.{sid}.args[{index}]"
            rebased = _candidate(field_name, arg)
            if rebased and rebased != str(arg):
                applied.append({"field": field_name, "from": str(arg), "to": rebased})
                new_args.append(rebased)
                changed_args = True
            else:
                new_args.append(str(arg))
        if changed_args:
            item["args"] = new_args
        new_servers.append(item)

    runtimes = list(getattr(settings, "agent_runtimes", None) or [])
    new_runtimes: list[dict[str, Any]] = []
    for runtime in runtimes:
        if not isinstance(runtime, dict):
            continue
        item = dict(runtime)
        runtime_id = str(item.get("id") or "agent-runtime")
        for field in ("command", "cwd"):
            field_name = f"agent_runtimes.{runtime_id}.{field}"
            rebased = _candidate(field_name, item.get(field))
            if rebased and rebased != str(item.get(field) or ""):
                applied.append({"field": field_name, "from": str(item.get(field)), "to": rebased})
                item[field] = rebased
        args = item.get("args") if isinstance(item.get("args"), list) else []
        new_args: list[str] = []
        changed_args = False
        for index, arg in enumerate(args):
            field_name = f"agent_runtimes.{runtime_id}.args[{index}]"
            rebased = _candidate(field_name, arg)
            if rebased and rebased != str(arg):
                applied.append({"field": field_name, "from": str(arg), "to": rebased})
                new_args.append(rebased)
                changed_args = True
            else:
                new_args.append(str(arg))
        if changed_args:
            item["args"] = new_args
        new_runtimes.append(item)

    logging = getattr(settings, "logging", None)
    new_logging = logging
    if logging is not None:
        log_dir = str(getattr(logging, "log_dir", "") or "")
        rebased_log = _candidate("logging.log_dir", log_dir)
        if rebased_log and rebased_log != log_dir:
            applied.append({"field": "logging.log_dir", "from": log_dir, "to": rebased_log})
            new_logging = replace(logging, log_dir=rebased_log)

    replacement_fields: dict[str, Any] = {
        "toolchain_paths": new_toolchain,
        "mcp_servers": new_servers,
        "logging": new_logging if new_logging is not None else logging,
    }
    if hasattr(settings, "agent_runtimes"):
        replacement_fields["agent_runtimes"] = new_runtimes
    updated = replace(settings, **replacement_fields)
    return applied, updated


def preview_data_root_change(
    source_root: str | Path,
    dest_root: str | Path,
    *,
    mode: str = "",
    toolchain_paths: dict[str, str] | None = None,
    mcp_servers: Iterable[dict[str, Any]] | None = None,
    agent_runtimes: Iterable[dict[str, Any]] | None = None,
    log_dir: str = "",
) -> dict[str, Any]:
    """Read-only validation + size / free-space / rewrite preview."""
    src = _abs(source_root)
    dst = _abs(dest_root)
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if _same_path(src, dst):
        blockers.append({"code": "same_path", "message": "Source and destination data roots are the same."})
    if _is_under(dst, src) and not _same_path(src, dst):
        blockers.append({"code": "dest_inside_source", "message": "Destination cannot be inside the current data root."})
    if _is_under(src, dst) and not _same_path(src, dst):
        blockers.append({"code": "source_inside_dest", "message": "Current data root cannot be inside the destination."})
    mode_norm = str(mode or "").strip().lower()
    if mode_norm == "switch":
        if not dst.exists():
            blockers.append({
                "code": "switch_destination_missing",
                "message": "Switch destination must already exist; it will not be created automatically.",
            })
        elif not dst.is_dir():
            blockers.append({
                "code": "switch_destination_not_directory",
                "message": "Switch destination must be an existing directory.",
            })

    subdir_report: list[dict[str, Any]] = []
    bytes_to_copy = 0
    for name in MANAGED_SUBDIRS:
        src_sub = src / name
        dst_sub = dst / name
        size = _tree_size_bytes(src_sub) if src_sub.exists() else 0
        bytes_to_copy += size
        dest_conflict = _dir_has_entries(dst_sub) and _dir_has_entries(src_sub)
        if dest_conflict:
            warnings.append({
                "code": "dest_subdir_nonempty",
                "message": f"Destination already has non-empty '{name}/'; copy/move will be blocked.",
                "subdir": name,
            })
        subdir_report.append({
            "name": name,
            "source_exists": src_sub.exists(),
            "source_bytes": size,
            "dest_exists": dst_sub.exists(),
            "dest_nonempty": _dir_has_entries(dst_sub),
            "conflict": dest_conflict,
        })

    active_log_dir = Path(log_dir).expanduser() if str(log_dir or "").strip() else src / SUBDIR_LOGS
    if _is_under(active_log_dir, src) and _dir_has_entries(active_log_dir):
        warnings.append({
            "code": "log_dir_not_transferred",
            "message": (
                "Existing log files stay at the old root because the running process holds them "
                "open; new records are written under the new root right after the migration."
            ),
            "path": str(_abs(active_log_dir)),
        })

    runtime_root = src / SUBDIR_TOOLS / "runtimes"
    if _dir_has_entries(runtime_root):
        warnings.append({
            "code": "in_use_runtime_files",
            "message": (
                "Managed runtime files the running process has open (for example Python "
                "__pycache__) stay at the old root and are recreated after restart."
            ),
            "path": str(_abs(runtime_root)),
        })

    free = _free_bytes(dst)
    if free is not None and bytes_to_copy > 0 and free < int(bytes_to_copy * 1.05):
        # Warning only — ``switch`` does not transfer bytes. copy/move enforce
        # this in ``apply_data_root_change``.
        warnings.append({
            "code": "insufficient_space",
            "message": "Destination volume may not have enough free space for a full copy/move.",
        })

    rewrites = collect_path_rewrites(
        old_root=src,
        new_root=dst,
        toolchain_paths=toolchain_paths,
        mcp_servers=mcp_servers,
        agent_runtimes=agent_runtimes,
        log_dir=log_dir,
        allow_managed_transfer=mode_norm in {"", "copy", "move"},
    )
    if rewrites["count"]:
        warnings.append({
            "code": "absolute_paths",
            "message": f"{rewrites['count']} absolute path(s) in settings point under the old root and will be rewritten.",
        })
    if rewrites["missing_destination_count"]:
        warnings.append({
            "code": "rewrite_destination_missing",
            "message": (
                f"{rewrites['missing_destination_count']} absolute path(s) cannot be rewritten "
                "because the corresponding destination does not exist."
            ),
        })

    return {
        "ok": not blockers,
        "source": str(src),
        "destination": str(dst),
        "bytes_to_transfer": bytes_to_copy,
        "free_bytes": free,
        "subdirs": subdir_report,
        "blockers": blockers,
        "warnings": warnings,
        "path_rewrites": rewrites,
        "modes": sorted(MIGRATE_MODES),
        "mode": mode_norm,
        "restart_required": True,
    }


def _copy_tree(
    src: Path,
    dst: Path,
    *,
    on_bytes: Callable[[int], None] | None = None,
    skipped: list[str] | None = None,
) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    skipped_rels = skipped if skipped is not None else []

    def _skip(source: str) -> str:
        raw = _strip_long_path(source)
        try:
            rel = Path(raw).relative_to(src).as_posix()
        except ValueError:
            rel = Path(raw).name
        skipped_rels.append(_normalize_rel(rel))
        return source

    def _copy_file(source: str, destination: str) -> str:
        src_p = _fs_copy_path(source)
        dst_p = _fs_copy_path(destination)
        try:
            copied = shutil.copy2(src_p, dst_p)
        except OSError as exc:
            if _is_unusable_copy_oserror(exc):
                try:
                    shutil.copyfile(src_p, dst_p)
                    copied = dst_p
                except OSError as exc2:
                    if _is_skippable_copy_oserror(exc2):
                        return _skip(source)
                    raise
            elif _is_locked_oserror(exc):
                return _skip(source)
            else:
                raise
        if on_bytes is not None:
            try:
                on_bytes(int(Path(_strip_long_path(source)).stat().st_size))
            except OSError:
                pass
        return copied

    if src.is_dir():
        shutil.copytree(
            src if os.name != "nt" else _fs_copy_path(src),
            dst if os.name != "nt" else _fs_copy_path(dst),
            dirs_exist_ok=False,
            symlinks=True,
            ignore=_ignore_ephemeral,
            copy_function=_copy_file,
        )
    elif src.is_file():
        if not _is_ephemeral_name(src.name):
            _copy_file(str(src), str(dst))
    else:
        raise OSError(f"source is not a file or directory: {src}")


def _sha256_file(path: Path, *, on_bytes: Callable[[int], None] | None = None) -> str:
    digest = hashlib.sha256()
    with open(_fs_copy_path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    return digest.hexdigest()


def _tree_manifest(
    root: Path,
    *,
    on_bytes: Callable[[int], None] | None = None,
    skipped_rels: Iterable[str] | None = None,
) -> dict[str, tuple[Any, ...]]:
    """Build a content manifest including files, empty dirs, and symlinks."""
    skipped = {
        _normalize_rel(item)
        for item in (skipped_rels or [])
        if _normalize_rel(item)
    }

    def _include(rel: str) -> bool:
        norm = _normalize_rel(rel)
        if not norm:
            return True
        return not _is_skippable_rel(norm) and norm not in skipped

    if root.is_symlink():
        return {"": ("symlink", os.readlink(root))}
    if root.is_file():
        return {"": ("file", int(root.stat().st_size), _sha256_file(root, on_bytes=on_bytes))}
    if not root.is_dir():
        raise OSError(f"cannot manifest missing tree: {root}")

    manifest: dict[str, tuple[Any, ...]] = {"": ("dir",)}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames
            if not _is_ephemeral_name(name)
            and not (
                _under_node_modules(dirpath)
                and name.lower() in _NODE_SKIP_DIRS
            )
        ]
        base = Path(dirpath)
        for name in sorted(dirnames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if not _include(rel):
                continue
            if path.is_symlink():
                manifest[rel] = ("symlink", os.readlink(path))
            else:
                manifest[rel] = ("dir",)
        for name in sorted(filenames):
            if _is_ephemeral_name(name):
                continue
            path = base / name
            rel = path.relative_to(root).as_posix()
            if not _include(rel):
                continue
            if path.is_symlink():
                manifest[rel] = ("symlink", os.readlink(path))
            else:
                manifest[rel] = (
                    "file",
                    int(path.stat().st_size),
                    _sha256_file(path, on_bytes=on_bytes),
                )
    return manifest


def _verify_tree(
    source: Path,
    destination: Path,
    *,
    on_bytes: Callable[[int], None] | None = None,
    skipped_rels: Iterable[str] | None = None,
) -> bool:
    return (
        _tree_manifest(source, on_bytes=on_bytes, skipped_rels=skipped_rels)
        == _tree_manifest(destination, on_bytes=on_bytes, skipped_rels=skipped_rels)
    )


def _remove_path(path: Path) -> None:
    def _onexc(_func: Any, _p: str, exc: BaseException) -> None:
        if _is_skippable_copy_oserror(exc):
            return
        raise exc

    if path.is_symlink() or path.is_file():
        try:
            path.unlink()
        except OSError as exc:
            if not _is_skippable_copy_oserror(exc):
                raise
        return
    if not path.exists():
        return
    try:
        shutil.rmtree(path, onexc=_onexc)
    except TypeError:
        shutil.rmtree(
            path,
            onerror=lambda func, p, info: _onexc(func, p, info[1]),
        )


def _rollback_transfer(transaction: dict[str, Any]) -> dict[str, Any]:
    removed: list[str] = []
    errors: list[str] = []
    destination = _abs(transaction.get("destination") or ".")
    for raw in reversed(list(transaction.get("created_destination_paths") or [])):
        path = _abs(raw)
        try:
            rel = path.relative_to(destination)
            if not rel.parts or rel.parts[0] not in PERSISTENT_DATA_SUBDIRS:
                raise ValueError("path is outside managed destination subdirectories")
            _remove_path(path)
            removed.append(str(path))
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    for raw in transaction.get("preexisting_empty_paths") or []:
        path = _abs(raw)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    if transaction.get("created_destination_root"):
        try:
            destination.rmdir()
        except OSError:
            pass
    return {"ok": not errors, "removed": removed, "errors": errors}


def transfer_managed_subdirs(
    source_root: Path,
    dest_root: Path,
    *,
    mode: str,
    transaction: dict[str, Any] | None = None,
    progress: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    """Stage, content-verify, then publish managed subdirectories.

    ``move`` deliberately performs the same verified copy as ``copy``. Source
    deletion is a separate finalization step after settings persistence.
    """
    if mode not in {"copy", "move"}:
        raise ValueError(f"unsupported transfer mode: {mode}")
    src = _abs(source_root)
    dst = _abs(dest_root)
    if dst.exists() and not dst.is_dir():
        raise ValueError("destination data root is not a directory")

    for name in MANAGED_SUBDIRS:
        src_sub = src / name
        dst_sub = dst / name
        if src_sub.exists() and _dir_has_entries(dst_sub):
            raise ValueError(f"destination already has non-empty '{name}/'")
        if src_sub.exists() and dst_sub.exists() and not dst_sub.is_dir():
            raise ValueError(f"destination already has non-directory '{name}'")

    dst.parent.mkdir(parents=True, exist_ok=True)
    staging = dst.parent / f".{dst.name}.argusai-migration-{uuid.uuid4().hex}"
    staging.mkdir(parents=False, exist_ok=False)
    tx: dict[str, Any] = {
        "mode": mode,
        "source": str(src),
        "destination": str(dst),
        "staging": str(staging),
        "created_destination_root": not dst.exists(),
        "created_destination_paths": [],
        "preexisting_empty_paths": [],
        "cleanup_sources": [],
        "skipped_by_subdir": {},
        "verified": False,
    }
    transferred: list[dict[str, Any]] = []
    total_bytes = sum(
        _tree_size_bytes(src / name)
        for name in MANAGED_SUBDIRS
        if (src / name).exists()
    )
    total_work = max(1, total_bytes * 3)
    completed_work = 0

    def _advance(amount: int, *, phase: str, subdir: str) -> None:
        nonlocal completed_work
        completed_work += max(0, int(amount or 0))
        _emit_progress(
            progress,
            phase=phase,
            percent=min(92, 2 + int((completed_work / total_work) * 90)),
            completed_bytes=min(completed_work, total_work),
            total_work_bytes=total_work,
            current_subdir=subdir,
        )

    try:
        # No destination data is touched until every source subtree has copied
        # and matched a SHA-256 manifest.
        for name in MANAGED_SUBDIRS:
            src_sub = src / name
            if not src_sub.exists():
                continue
            staged_sub = staging / name
            skipped_rels: list[str] = []
            _emit_progress(
                progress,
                phase="copying",
                percent=min(92, 2 + int((completed_work / total_work) * 90)),
                completed_bytes=completed_work,
                total_work_bytes=total_work,
                current_subdir=name,
            )
            _copy_tree(
                src_sub,
                staged_sub,
                on_bytes=lambda amount, current=name: _advance(
                    amount,
                    phase="copying",
                    subdir=current,
                ),
                skipped=skipped_rels,
            )
            _emit_progress(
                progress,
                phase="verifying",
                percent=min(92, 2 + int((completed_work / total_work) * 90)),
                completed_bytes=completed_work,
                total_work_bytes=total_work,
                current_subdir=name,
            )
            if not _verify_tree(
                src_sub,
                staged_sub,
                on_bytes=lambda amount, current=name: _advance(
                    amount,
                    phase="verifying",
                    subdir=current,
                ),
                skipped_rels=skipped_rels,
            ):
                raise OSError(f"verification failed for staged '{name}/'")
            tx["skipped_by_subdir"][name] = list(skipped_rels)

        _emit_progress(progress, phase="publishing", percent=94)
        dst.mkdir(parents=True, exist_ok=True)
        for name in MANAGED_SUBDIRS:
            src_sub = src / name
            dst_sub = dst / name
            staged_sub = staging / name
            if src_sub.exists():
                if dst_sub.exists():
                    try:
                        dst_sub.rmdir()
                    except OSError as exc:
                        raise ValueError(f"cannot replace empty destination '{name}/': {exc}") from exc
                    tx["preexisting_empty_paths"].append(str(dst_sub))
                os.replace(staged_sub, dst_sub)
                tx["created_destination_paths"].append(str(dst_sub))
                if mode == "move":
                    tx["cleanup_sources"].append(str(src_sub))
                transferred.append({
                    "name": name,
                    "action": "copied" if mode == "copy" else "copied_pending_cleanup",
                    "source": str(src_sub),
                    "destination": str(dst_sub),
                    "bytes": _tree_size_bytes(dst_sub),
                    "verified": True,
                    "skipped": list(tx["skipped_by_subdir"].get(name) or []),
                })
            elif not dst_sub.exists():
                dst_sub.mkdir()
                tx["created_destination_paths"].append(str(dst_sub))

        tx["verified"] = True
        if transaction is not None:
            transaction.clear()
            transaction.update(tx)
        return transferred
    except Exception:
        _rollback_transfer(tx)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def rollback_data_root_change(result: dict[str, Any]) -> dict[str, Any]:
    """Remove destination artifacts if settings persistence did not commit."""
    transaction = result.get("_transfer_transaction")
    if not isinstance(transaction, dict):
        return {"ok": True, "removed": [], "errors": []}
    return _rollback_transfer(transaction)


def finalize_data_root_move(
    result: dict[str, Any],
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Delete verified move sources after the new settings have been saved."""
    if result.get("mode") != "move":
        return {"ok": True, "cleaned": [], "errors": []}
    transaction = result.get("_transfer_transaction")
    if not isinstance(transaction, dict) or not transaction.get("verified"):
        return {"ok": False, "cleaned": [], "errors": ["missing verified transfer transaction"]}

    source_root = _abs(transaction.get("source") or ".")
    destination_root = _abs(transaction.get("destination") or ".")
    skipped_by_subdir = dict(transaction.get("skipped_by_subdir") or {})
    cleaned: list[str] = []
    errors: list[str] = []
    cleanup_sources = list(transaction.get("cleanup_sources") or [])
    total_verify_bytes = max(
        1,
        sum(_tree_size_bytes(_abs(raw)) for raw in cleanup_sources) * 2,
    )
    verified_bytes = 0

    def _advance(amount: int, *, subdir: str) -> None:
        nonlocal verified_bytes
        verified_bytes += max(0, int(amount or 0))
        _emit_progress(
            progress,
            phase="cleaning",
            percent=min(99, 95 + int((verified_bytes / total_verify_bytes) * 4)),
            completed_bytes=min(verified_bytes, total_verify_bytes),
            total_work_bytes=total_verify_bytes,
            current_subdir=subdir,
        )

    for raw in cleanup_sources:
        source = _abs(raw)
        try:
            rel = source.relative_to(source_root)
            if len(rel.parts) != 1 or rel.parts[0] not in PERSISTENT_DATA_SUBDIRS:
                raise ValueError("source is outside managed subdirectories")
            destination = destination_root / rel
            _emit_progress(
                progress,
                phase="cleaning",
                percent=min(99, 95 + int((verified_bytes / total_verify_bytes) * 4)),
                current_subdir=rel.parts[0],
            )
            if not destination.exists() or not _verify_tree(
                source,
                destination,
                on_bytes=lambda amount, current=rel.parts[0]: _advance(
                    amount,
                    subdir=current,
                ),
                skipped_rels=skipped_by_subdir.get(rel.parts[0]) or [],
            ):
                raise OSError("source changed after transfer or destination verification failed")
            _remove_path(source)
            cleaned.append(str(source))
        except Exception as exc:
            errors.append(f"{source}: {exc}")
    return {"ok": not errors, "cleaned": cleaned, "errors": errors}


def apply_data_root_change(
    settings: Any,
    new_root: str | Path,
    *,
    mode: str,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Transfer data (optional), rewrite absolute paths, return updated settings + receipt.

    Does not persist — caller must ``settings_mgr.save``.
    """
    mode_norm = str(mode or "").strip().lower()
    if mode_norm not in MIGRATE_MODES:
        return {"ok": False, "error": "mode must be one of: copy, move, switch"}

    old_root = _abs(getattr(settings, "data_root"))
    dest = _abs(new_root)
    _emit_progress(progress, phase="preparing", percent=1)
    preview = preview_data_root_change(
        old_root,
        dest,
        mode=mode_norm,
        toolchain_paths=getattr(settings, "toolchain_paths", None),
        mcp_servers=getattr(settings, "mcp_servers", None),
        agent_runtimes=getattr(settings, "agent_runtimes", None),
        log_dir=str(getattr(getattr(settings, "logging", None), "log_dir", "") or ""),
    )
    if not preview["ok"]:
        return {
            "ok": False,
            "error": preview["blockers"][0]["message"] if preview["blockers"] else "migration blocked",
            "preview": preview,
        }

    if mode_norm in {"copy", "move"}:
        if any(w.get("code") == "insufficient_space" for w in preview.get("warnings") or []):
            return {
                "ok": False,
                "error": "Destination volume does not have enough free space for a full copy/move.",
                "preview": preview,
            }
        conflicts = [row for row in preview["subdirs"] if row.get("conflict")]
        if conflicts:
            names = ", ".join(row["name"] for row in conflicts)
            return {
                "ok": False,
                "error": f"destination already has data in: {names}",
                "preview": preview,
            }
        transaction: dict[str, Any] = {}
        try:
            transferred = transfer_managed_subdirs(
                old_root,
                dest,
                mode=mode_norm,
                transaction=transaction,
                progress=progress,
            )
        except (OSError, ValueError, shutil.Error) as exc:
            return {"ok": False, "error": str(exc), "preview": preview}
    else:
        transferred = []
        transaction = {}

    skipped_rewrites: list[dict[str, str]] = []
    try:
        _emit_progress(
            progress,
            phase="rewriting",
            percent=96 if mode_norm in {"copy", "move"} else 70,
        )
        applied, updated = apply_path_rewrites(
            settings,
            old_root,
            dest,
            skipped=skipped_rewrites,
        )
        if mode_norm == "switch" and skipped_rewrites:
            fields = ", ".join(item["field"] for item in skipped_rewrites[:5])
            return {
                "ok": False,
                "error": f"switch would rewrite paths to missing destinations: {fields}",
                "preview": preview,
                "path_rewrite_skipped": skipped_rewrites,
            }
        launcher_repairs, updated = repair_mcp_venv_launchers(updated)
        updated = replace(updated, data_root=str(dest))
    except Exception as exc:
        rollback = _rollback_transfer(transaction) if transaction else None
        return {
            "ok": False,
            "error": f"failed to prepare migrated settings: {exc}",
            "preview": preview,
            "rollback": rollback,
        }

    return {
        "ok": True,
        "mode": mode_norm,
        "source": str(old_root),
        "destination": str(dest),
        "transferred": transferred,
        "path_rewrites": applied,
        "path_rewrite_skipped": skipped_rewrites,
        "mcp_launcher_repairs": launcher_repairs,
        "restart_required": True,
        "settings": updated,
        "preview": preview,
        "_transfer_transaction": transaction,
    }
