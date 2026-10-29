"""Export a Reverse Job into a self-contained directory or zip.

Built on top of ``ReverseJobStore`` — does NOT touch the live job
folder, only copies a subset out so users can share the case with
someone else (or attach it to a ticket). The ``decompiled/`` tree is
opt-in because it can easily be hundreds of MB.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Any

from argusai.core.reverse.reverse_jobs import ReverseJobStore


# Subdirectories worth shipping by default. ``decompiled/`` is
# intentionally NOT here — it's gated behind ``include_decompiled``.
_DEFAULT_SUBDIRS: tuple[str, ...] = (
    "artifacts",
    "hooks",
    "findings",
    "reproduce",
    "oracles",
)

# Top-level files always copied (small JSON / audit log).
_TOP_LEVEL_FILES: tuple[str, ...] = ("job.json", "events.jsonl")

# Hard upper bound to stop a runaway export from filling the disk.
# Counted across all copied bytes; tripping it aborts the export.
_MAX_EXPORT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


def _safe_export_name(job_id: str, title: str) -> str:
    raw = f"{job_id}-{title}" if title else job_id
    out: list[str] = []
    for ch in raw[:120]:
        if ch.isalnum() or ch in "-_.":
            out.append(ch)
        elif ch in " /\\":
            out.append("-")
    cleaned = "".join(out).strip(" -.") or job_id
    return cleaned


def _dedupe_dir(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.name}-{i}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"too many duplicate export targets: {path}")


def _copy_tree(src: Path, dst: Path, budget_left: int) -> tuple[int, int, int]:
    """Copy ``src`` into ``dst`` while tracking byte budget.

    Returns ``(files_copied, bytes_copied, new_budget_left)``. Raises
    ``OSError`` if the budget would be exceeded.
    """
    files = 0
    total = 0
    if not src.exists():
        return files, total, budget_left
    if src.is_file():
        size = src.stat().st_size
        if size > budget_left:
            raise OSError(f"export exceeds {_MAX_EXPORT_BYTES} byte budget")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return 1, size, budget_left - size
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.rglob("*"):
        if not child.is_file():
            continue
        rel = child.relative_to(src)
        size = child.stat().st_size
        if size > budget_left:
            raise OSError(f"export exceeds {_MAX_EXPORT_BYTES} byte budget")
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(child, out)
        files += 1
        total += size
        budget_left -= size
    return files, total, budget_left


def export_reverse_job(
    store: ReverseJobStore,
    job_id: str,
    *,
    dest_dir: str | Path | None = None,
    include_decompiled: bool = False,
    as_zip: bool = True,
    extra_subdirs: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Export a reverse job. Returns a metadata dict with the resulting path.

    Args:
        store: Live ``ReverseJobStore`` instance.
        job_id: Job to export. The job must exist (trashed jobs allowed).
        dest_dir: Where to drop the export. ``None`` means
            ``<store.root>/_exports``. When ``as_zip=False`` the export
            becomes ``<dest_dir>/<job-id>-<title>/``; when ``True`` it
            becomes ``<dest_dir>/<job-id>-<title>.zip``.
        include_decompiled: Copy the ``decompiled/`` subtree too. Off by
            default because JADX / APKTool output can be very large.
        as_zip: Zip the export when true (default) — preserves layout
            inside the archive and writes to ``<name>.zip``.
        extra_subdirs: Additional subdir names to include beyond the
            default ``artifacts / hooks / findings / reproduce / oracles``.

    Raises ``FileNotFoundError`` if the job is missing, ``OSError`` on
    disk/budget failures.
    """
    job = store.get(str(job_id or ""), include_trashed=True)
    if job is None:
        raise FileNotFoundError(f"reverse job not found: {job_id}")

    job_dir = Path(str(job.get("_dir") or "")).resolve()
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job directory missing: {job_dir}")

    base_dest = Path(dest_dir).expanduser().resolve() if dest_dir else (Path(store.root) / "_exports")
    base_dest.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_export_name(str(job.get("id") or job_id), str(job.get("title") or ""))

    subdirs = list(_DEFAULT_SUBDIRS) + [s for s in extra_subdirs if s and s not in _DEFAULT_SUBDIRS]
    if include_decompiled:
        subdirs.append("decompiled")

    if as_zip:
        archive_path = _dedupe_dir(base_dest / f"{safe_name}.zip")
        files_copied, bytes_copied = _zip_job(job_dir, archive_path, subdirs)
        return {
            "ok": True,
            "job_id": str(job.get("id") or job_id),
            "path": str(archive_path),
            "format": "zip",
            "files": files_copied,
            "bytes": bytes_copied,
            "include_decompiled": bool(include_decompiled),
        }

    target_dir = _dedupe_dir(base_dest / safe_name)
    target_dir.mkdir(parents=True, exist_ok=True)
    files_copied = 0
    bytes_copied = 0
    budget_left = _MAX_EXPORT_BYTES
    for name in _TOP_LEVEL_FILES:
        src = job_dir / name
        if not src.is_file():
            continue
        f, b, budget_left = _copy_tree(src, target_dir / name, budget_left)
        files_copied += f
        bytes_copied += b
    for sub in subdirs:
        src = job_dir / sub
        if not src.is_dir():
            continue
        f, b, budget_left = _copy_tree(src, target_dir / sub, budget_left)
        files_copied += f
        bytes_copied += b

    return {
        "ok": True,
        "job_id": str(job.get("id") or job_id),
        "path": str(target_dir),
        "format": "dir",
        "files": files_copied,
        "bytes": bytes_copied,
        "include_decompiled": bool(include_decompiled),
    }


def _zip_job(job_dir: Path, archive_path: Path, subdirs: list[str]) -> tuple[int, int]:
    files_copied = 0
    bytes_copied = 0
    budget_left = _MAX_EXPORT_BYTES
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for name in _TOP_LEVEL_FILES:
            src = job_dir / name
            if not src.is_file():
                continue
            size = src.stat().st_size
            if size > budget_left:
                raise OSError(f"export exceeds {_MAX_EXPORT_BYTES} byte budget")
            zf.write(src, arcname=name)
            files_copied += 1
            bytes_copied += size
            budget_left -= size
        for sub in subdirs:
            src_dir = job_dir / sub
            if not src_dir.is_dir():
                continue
            for child in src_dir.rglob("*"):
                if not child.is_file():
                    continue
                size = child.stat().st_size
                if size > budget_left:
                    raise OSError(f"export exceeds {_MAX_EXPORT_BYTES} byte budget")
                arcname = f"{sub}/{child.relative_to(src_dir).as_posix()}"
                zf.write(child, arcname=arcname)
                files_copied += 1
                bytes_copied += size
                budget_left -= size
    return files_copied, bytes_copied


__all__ = ["export_reverse_job"]
