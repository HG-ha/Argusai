"""Export assistant deliverables to a user-selected workspace."""

from __future__ import annotations

import json
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any


REPORT_FILES = {
    "html": "report.html",
    "md": "report.md",
    "json": "report.json",
    "findings": "findings.jsonl",
}
CUSTOMER_REPORT_FORMATS = ["html", "md"]
INTERNAL_REPORT_FORMATS = ["html", "md", "json", "findings"]
SKIP_DIRS = {".venv", ".tmp", "__pycache__"}
SKIP_FILES = {"audit.jsonl"}
VERIFICATION_SCRIPT_EXTS = {".py", ".ps1", ".sh", ".cmd", ".bat"}
DELIVERABLE_EXTS = {
    ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".html", ".htm",
    ".py", ".js", ".ts", ".css", ".ps1", ".sh", ".bat", ".cmd",
    ".log", ".xml", ".csv", ".har",
}
DELIVERABLE_NAME_HINTS = {
    "poc", "repro", "reproduce", "exploit", "verify", "lab", "report",
    "analysis", "finding", "evidence", "trace", "request", "response",
    "readme", "requirements", "run",
}


def _is_verification_snippet(path: Path) -> bool:
    """Temporary snippet scripts are part of the audit trail.

    ``run_python_snippet`` / ``run_shell_snippet`` store code under
    ``.tmp/snippet-*.{py,ps1,sh,cmd,bat}``. The rest of ``.tmp`` is runtime
    scratch space, but these scripts explain how a lead was validated and
    should be exported by default.
    """
    parts = set(path.parts)
    return (
        ".tmp" in parts
        and path.name.lower().startswith("snippet-")
        and path.suffix.lower() in VERIFICATION_SCRIPT_EXTS
    )


def _now_slug() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def _copy_file(src: Path, dst: Path) -> dict[str, Any]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {"source": str(src), "path": str(dst), "size": dst.stat().st_size}


def _is_skipped_runtime_path(rel: Path) -> bool:
    if rel.name.lower() in SKIP_FILES:
        return True
    return any(part in SKIP_DIRS or str(part).startswith(".") for part in rel.parts)


def _looks_deliverable(path: Path) -> tuple[bool, str]:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if name in SKIP_FILES:
        return False, "sandbox audit log is internal, not a deliverable"
    if _is_verification_snippet(path):
        return True, "verification snippet script"
    if suffix not in DELIVERABLE_EXTS:
        return False, f"extension {suffix or '(none)'} not deliverable"
    if suffix in {".py", ".md", ".html", ".json", ".jsonl", ".har"}:
        return True, f"deliverable extension {suffix}"
    if any(hint in name for hint in DELIVERABLE_NAME_HINTS):
        return True, "name matches deliverable hint"
    return False, "not selected by default"


def _zip_dir(src_dir: Path, zip_path: Path, *, overwrite: bool = False) -> dict[str, Any]:
    if zip_path.exists() and not overwrite:
        return {"ok": False, "error": f"zip already exists: {zip_path}", "path": str(zip_path)}
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path.is_file() and path.resolve() != zip_path.resolve():
                zf.write(path, path.relative_to(src_dir).as_posix())
                count += 1
    return {"ok": True, "path": str(zip_path), "file_count": count, "size": zip_path.stat().st_size}


def export_task_report(
    task: Any,
    out_dir: Path,
    formats: list[str] | None = None,
    overwrite: bool = False,
    create_zip: bool = False,
) -> dict[str, Any]:
    """Copy rendered task reports into ``out_dir``.

    The default is the customer-facing handoff set (HTML + Markdown). Raw
    machine-readable data (``report.json`` / ``findings.jsonl``) is still
    available when callers explicitly request ``json`` or ``findings``.
    """
    selected = formats or CUSTOMER_REPORT_FORMATS
    copied: list[dict[str, Any]] = []
    missing: list[str] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for fmt in selected:
        key = str(fmt or "").strip().lower()
        fname = REPORT_FILES.get(key)
        if not fname:
            missing.append(key)
            continue
        src = Path(task.root) / fname
        if not src.exists():
            missing.append(key)
            continue
        dst = out_dir / fname
        if dst.exists() and not overwrite:
            missing.append(f"{key}:exists")
            continue
        copied.append(_copy_file(src, dst))
    result = {
        "ok": bool(copied),
        "out_dir": str(out_dir),
        "copied": copied,
        "missing_or_skipped": missing,
        "profile": "custom_fmts" if formats else "customer",
    }
    if not formats:
        result["omitted_internal_formats"] = ["json", "findings"]
        existing_internal = [REPORT_FILES[key] for key in ("json", "findings") if (out_dir / REPORT_FILES[key]).exists()]
        if existing_internal:
            result["warnings"] = [
                "Existing internal report files were left untouched: "
                + ", ".join(existing_internal)
                + ". Use a fresh export directory for a clean customer handoff."
            ]
    if create_zip and copied:
        result["zip"] = _zip_dir(out_dir, out_dir.with_suffix(".zip"), overwrite=overwrite)
    return result


def copy_sandbox_tree(
    sandbox_root: Path,
    out_dir: Path,
    *,
    max_files: int = 200,
    overwrite: bool = False,
    selection_mode: str = "auto",
) -> list[dict[str, Any]]:
    """Copy session sandbox files into an export directory, skipping runtime internals."""
    copied: list[dict[str, Any]] = []
    if not sandbox_root.exists():
        return copied
    for path in sorted(sandbox_root.rglob("*")):
        if len(copied) >= max_files:
            break
        rel = path.relative_to(sandbox_root)
        if path.name.lower() in SKIP_FILES:
            continue
        if not path.is_file():
            continue
        is_verification_snippet = _is_verification_snippet(rel)
        if not is_verification_snippet and any(part in SKIP_DIRS or str(part).startswith(".") for part in rel.parts):
            continue
        selected, reason = _looks_deliverable(path)
        if selection_mode == "auto" and not selected:
            continue
        dst_rel = Path("verification-scripts") / rel.name if is_verification_snippet else rel
        dst = out_dir / dst_rel
        if dst.exists() and not overwrite:
            continue
        item = _copy_file(path, dst)
        item["selection_reason"] = reason if selection_mode == "auto" else "selection_mode=all"
        copied.append(item)
    return copied


def _coerce_selected_paths(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    text = str(value or "")
    if not text.strip():
        return []
    try:
        raw = json.loads(text)
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
    except Exception:
        pass
    return [line.strip() for line in text.replace(";", "\n").splitlines() if line.strip()]


def _resolve_selected_sandbox_path(sandbox_root: Path, raw: str) -> Path | None:
    try:
        root = sandbox_root.resolve()
        p = Path(str(raw or "").strip().strip('"').strip("'")).expanduser()
        candidate = p.resolve() if p.is_absolute() else (root / p).resolve()
        candidate.relative_to(root)
        return candidate
    except (OSError, ValueError):
        return None


def _iter_selected_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        return [p for p in sorted(path.rglob("*")) if p.is_file()]
    return []


def copy_selected_sandbox_paths(
    sandbox_root: Path,
    out_dir: Path,
    selected_paths: Any,
    *,
    max_files: int = 200,
    overwrite: bool = False,
) -> list[dict[str, Any]]:
    """Copy AI-selected sandbox files/directories into an export directory."""
    copied: list[dict[str, Any]] = []
    root = sandbox_root.resolve()
    seen: set[str] = set()
    for raw in _coerce_selected_paths(selected_paths):
        if len(copied) >= max_files:
            break
        selected = _resolve_selected_sandbox_path(root, raw)
        if selected is None or not selected.exists():
            continue
        for path in _iter_selected_files(selected):
            if len(copied) >= max_files:
                break
            try:
                rel = path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            is_verification_snippet = _is_verification_snippet(rel)
            if not is_verification_snippet and _is_skipped_runtime_path(rel):
                continue
            key = rel.as_posix()
            if key in seen:
                continue
            seen.add(key)
            dst_rel = Path("verification-scripts") / rel.name if is_verification_snippet else rel
            dst = out_dir / dst_rel
            if dst.exists() and not overwrite:
                continue
            item = _copy_file(path, dst)
            item["selection_reason"] = "explicitly selected by AI"
            item["selected_from"] = raw
            copied.append(item)
    return copied


def write_markdown(path: Path, title: str, body: str, overwrite: bool = False) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return {"path": str(path), "skipped": True, "reason": "exists"}
    text = f"# {title}\n\n{(body or '').strip()}\n"
    path.write_text(text, encoding="utf-8", newline="")
    return {"path": str(path), "size": path.stat().st_size}


def _build_dynamic_readme(
    *,
    has_reports: bool,
    has_workspace: bool,
    has_reproduction: bool,
    has_analysis: bool,
    has_notes: bool,
    has_manifest: bool,
) -> str:
    """Build a README body that only lists artefacts that actually exist.

    The previous template hard-coded every possible section (REPRODUCTION /
    ANALYSIS / NOTES / reports / manifest) even when none of them were
    written. That produced misleading directories where the README
    promised files that weren't there. Now the README is conditionally
    composed from the set of artefacts that the export actually produced;
    if nothing useful was produced, the README is skipped altogether by
    the caller.
    """

    lines: list[str] = [
        "This directory was exported by the assistant for review and handoff.",
    ]
    sections: list[str] = []
    if has_reports:
        sections.append("- `reports/`: customer-facing scan reports (HTML and Markdown by default).")
    if has_workspace:
        sections.append(
            "- `workspace/`: session sandbox files (PoC code, lab harnesses, verification scripts, raw evidence, etc.)."
        )
    if has_reproduction:
        sections.append("- `REPRODUCTION.md`: reproduction flow.")
    if has_analysis:
        sections.append("- `ANALYSIS.md`: reasoning and analysis notes.")
    if has_notes:
        sections.append("- `NOTES.md`: extra notes.")
    if has_manifest:
        sections.append("- `manifest.json`: machine-readable export manifest.")
    if sections:
        lines.append("")
        lines.append("## Contents")
        lines.append("")
        lines.extend(sections)
    return "\n".join(lines)


def export_workspace_bundle(
    *,
    out_dir: Path,
    task: Any | None = None,
    sandbox_root: Path | None = None,
    workflow_md: str = "",
    analysis_md: str = "",
    notes_md: str = "",
    include_reports: bool = True,
    include_sandbox: bool = True,
    include_readme: bool | None = None,
    include_manifest: bool = False,
    overwrite: bool = False,
    max_sandbox_files: int = 200,
    selection_mode: str = "auto",
    sandbox_paths: Any = None,
    create_zip: bool = False,
) -> dict[str, Any]:
    """Create a deliverable workspace with reports, PoC files and (optional) notes.

    Defaults are tuned for what the user actually receives: just the
    ``workspace/`` (and ``reports/`` when a task is provided). Two
    historically-noisy artefacts are now opt-in:

    - ``include_manifest`` defaults to ``False``. ``manifest.json`` is a
      machine-readable record of "which files I just copied" — no part
      of this codebase reads it back, and human reviewers don't want it.
      Enable explicitly when piping the export into automation.
    - ``include_readme`` defaults to "auto" (``None``): the directory
      index is only written when there is a curated document inside
      (REPRODUCTION / ANALYSIS / NOTES / reports). If the export is just
      ``workspace/``, a README that says "look inside workspace/" is
      pure noise and is skipped. Pass ``True`` to force it, ``False`` to
      forbid it.

    Other behavioural notes worth knowing as a caller:

    - The README is generated dynamically and only lists artefacts that
      were actually written.
    - An existing user-edited README is no longer clobbered: if
      ``overwrite=False`` and ``README.md`` already exists we leave it
      alone.
    """

    out_dir.mkdir(parents=True, exist_ok=True)
    exported: dict[str, Any] = {
        "root": str(out_dir),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "reports": None,
        "sandbox_files": [],
        "documents": [],
    }
    if include_reports and task is not None:
        exported["reports"] = export_task_report(task, out_dir / "reports", overwrite=overwrite)
    if include_sandbox and sandbox_root is not None:
        explicit_paths = _coerce_selected_paths(sandbox_paths)
        if explicit_paths:
            exported["sandbox_files"] = copy_selected_sandbox_paths(
                sandbox_root,
                out_dir / "workspace",
                explicit_paths,
                max_files=max_sandbox_files,
                overwrite=overwrite,
            )
            exported["sandbox_selection_mode"] = "explicit"
        else:
            exported["sandbox_files"] = copy_sandbox_tree(
                sandbox_root,
                out_dir / "workspace",
                max_files=max_sandbox_files,
                overwrite=overwrite,
                selection_mode=selection_mode,
            )
            exported["sandbox_selection_mode"] = selection_mode
    docs = [
        ("REPRODUCTION.md", "复现流程", workflow_md),
        ("ANALYSIS.md", "分析思路", analysis_md),
        ("NOTES.md", "补充说明", notes_md),
    ]
    written_docs: set[str] = set()
    for filename, title, body in docs:
        if body and str(body).strip():
            doc_info = write_markdown(out_dir / filename, title, body, overwrite=overwrite)
            exported["documents"].append(doc_info)
            if not doc_info.get("skipped"):
                written_docs.add(filename)

    report_info = exported.get("reports")
    has_reports = bool(isinstance(report_info, dict) and report_info.get("copied"))
    has_workspace = bool(exported.get("sandbox_files"))
    has_reproduction = "REPRODUCTION.md" in written_docs or (out_dir / "REPRODUCTION.md").exists()
    has_analysis = "ANALYSIS.md" in written_docs or (out_dir / "ANALYSIS.md").exists()
    has_notes = "NOTES.md" in written_docs or (out_dir / "NOTES.md").exists()

    if include_readme is None:
        # Auto mode: a README is only useful when there's a curated doc
        # to point at. ``workspace/`` alone doesn't need a README.
        should_write_readme = has_reports or has_reproduction or has_analysis or has_notes
    else:
        should_write_readme = bool(include_readme)
    if should_write_readme:
        readme_path = out_dir / "README.md"
        if readme_path.exists() and not overwrite:
            exported["documents"].insert(0, {"path": str(readme_path), "skipped": True, "reason": "exists"})
        else:
            readme_body = _build_dynamic_readme(
                has_reports=has_reports,
                has_workspace=has_workspace,
                has_reproduction=has_reproduction,
                has_analysis=has_analysis,
                has_notes=has_notes,
                has_manifest=include_manifest,
            )
            exported["documents"].insert(
                0,
                write_markdown(readme_path, "Exported Security Workspace", readme_body, overwrite=True),
            )
    if include_manifest:
        manifest = out_dir / "manifest.json"
        manifest.write_text(json.dumps(exported, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        exported["manifest"] = str(manifest)
    warnings: list[str] = []
    if include_reports and task is None:
        warnings.append("include_reports=true but no task_id/task was provided, so no scan reports were copied.")
    elif include_reports and isinstance(report_info, dict) and not report_info.get("copied"):
        warnings.append("No rendered task report files were found to copy.")
    if include_sandbox and not exported.get("sandbox_files"):
        warnings.append("No deliverable sandbox files were selected. Generate PoC/report/notes first or use selection_mode='all'.")
    if warnings:
        exported["warnings"] = warnings
    if create_zip:
        exported["zip"] = _zip_dir(out_dir, out_dir.with_suffix(".zip"), overwrite=overwrite)
    return {"ok": True, **exported}


def default_export_dir(base: Path, name: str = "deliverable") -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in (name or "deliverable")).strip(".-") or "deliverable"
    return base / f"{safe}-{_now_slug()}"
