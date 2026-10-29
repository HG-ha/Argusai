"""Assistant tools for mobile/native reverse static and dynamic analysis."""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .browser_reverse import _resolve_reverse_store
from .common import *

from argusai.core.infra.explorer import reveal_path
from argusai.core.infra.win_stdio import quiet_subprocess_kwargs
from argusai.core.reverse.android_static import analyze_apk_path
from argusai.core.reverse.android_toolchain import (
    run_adb_devices,
    run_adb_install,
    run_adb_logcat,
    run_adb_pull,
    run_adb_push,
    run_apk_sign,
    run_apktool_build,
    run_apktool_decode,
    run_jadx_decompile,
    run_objection_command,
    validate_apk_package,
)
from argusai.core.reverse.binary_toolchain import (
    run_capa_analyze,
    run_yara_scan,
)
from argusai.core.reverse.binary_patch import apply_binary_patches
from argusai.core.reverse.ghidra_runtime import (
    decompile_function as run_ghidra_decompile,
    disassemble_function as run_ghidra_disassemble,
    list_functions as run_ghidra_list_functions,
)
from argusai.core.reverse.elf_static import analyze_elf_path
from argusai.core.reverse.ios_static import analyze_ipa_path, extract_main_binary
from argusai.core.reverse.ios_toolchain import run_class_dump
from argusai.core.reverse.mobile_frida_recipes import get_recipe, list_recipes
from argusai.core.reverse.native_common import sha256_file
from argusai.core.reverse.reverse_artifacts import (
    ReverseArtifactStore,
    artifact_ref,
    resolve_artifact_path,
)
from argusai.core.reverse.reverse_job_export import export_reverse_job
from argusai.core.reverse.frida_runtime import get_frida_manager, run_frida, save_log_artifact
from argusai.core.reverse.toolchain import (
    clear_toolchain_cache,
    detect_tool,
    scan_toolchain,
    sync_path_overrides_from_settings,
    tool_unavailable_error,
)
from argusai.core.reverse.windows_static import analyze_pe_path


def _resolve_sample_path(api: Any, kwargs: dict[str, Any], path: str) -> Path:
    text = str(path or "").strip()
    if not text:
        raise ValueError("path is required")
    p = Path(text).expanduser()
    if p.is_file():
        return p.resolve()
    return Path(_resolve_session_input_path(api, kwargs, text)).resolve()


def _artifact_path(store: Any, job_id: str, artifact_id: str) -> tuple[Path, dict[str, Any]]:
    art = store.read_artifact(job_id, artifact_id, max_bytes=100 * 1024 * 1024)
    if art is None:
        raise FileNotFoundError(f"artifact not found: {artifact_id}")
    path = Path(str(art.get("path_abs") or ""))
    if not path.is_file():
        raise FileNotFoundError(f"artifact file missing: {artifact_id}")
    return path, art


def _guess_kind_from_path(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".apk": "apk",
        ".aab": "aab",
        ".xapk": "xapk",
        ".ipa": "ipa",
        ".exe": "pe",
        ".dll": "pe",
        ".sys": "pe",
        ".so": "elf",
        ".elf": "elf",
    }.get(ext, "other")


def _attach_analysis_to_job(
    store: Any,
    job_id: str,
    analysis: dict[str, Any],
    *,
    tool: str,
) -> dict[str, Any]:
    if not job_id or not analysis.get("ok"):
        return {}
    job = store.get(job_id)
    if job is None:
        return {}
    payload = json.dumps(analysis, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    rec = store.add_artifact(
        job_id,
        content=payload,
        kind="trace",
        filename=f"analysis-{tool}.json",
        tool=tool,
        note=f"static analysis summary ({tool})",
    )
    meta_patch: dict[str, Any] = {
        "sha256": analysis.get("sha256"),
        "size": analysis.get("size"),
        "format": analysis.get("format"),
    }
    if analysis.get("format") == "apk":
        packages = ((analysis.get("manifest") or {}).get("package_candidates") or [])
        if packages:
            meta_patch["package"] = packages[0]
    if analysis.get("format") == "ipa":
        bundle = ((analysis.get("info_plist") or {}).get("summary") or {}).get("bundle_id")
        if bundle:
            meta_patch["bundle_id"] = bundle
    static_summary = dict((job.get("analysis_summary") or {}).get("static") or {})
    static_summary.update({"done": True, "tools": [tool], "updated_at": analysis.get("analyzed_at", "")})
    store.update(
        job_id,
        target_meta=meta_patch,
        analysis_summary={**(job.get("analysis_summary") or {}), "static": static_summary},
    )
    if analysis.get("risk_tags"):
        store.update(job_id, risk_summary={"overall": "review", "tags": analysis.get("risk_tags"), "notes": ""})
    return {"artifact": rec, "artifact_ref": artifact_ref(job_id, rec["id"])}


def _sync_toolchain_from_api(api: Any) -> None:
    try:
        sync_path_overrides_from_settings(api.settings_mgr.get())
    except Exception:
        pass


def _preflight_cli_tool(api: Any, tool: str) -> dict[str, Any] | None:
    """Return a blocked error dict when ``tool`` is missing, else None."""
    _sync_toolchain_from_api(api)
    clear_toolchain_cache()
    info = detect_tool(tool)
    if info.get("available"):
        return None
    return tool_unavailable_error(tool, info=info)


def _preflight_any_cli_tool(api: Any, *tools: str) -> dict[str, Any] | None:
    """Return blocked error only when none of ``tools`` is available."""
    _sync_toolchain_from_api(api)
    clear_toolchain_cache()
    for tool in tools:
        if detect_tool(tool).get("available"):
            return None
    first = str(tools[0] if tools else "")
    return tool_unavailable_error(first, info=detect_tool(first))


def _attach_toolchain_scan(api: Any, store: Any, job_id: str) -> dict[str, Any]:
    """Probe host toolchain and persist into ``job.toolchain_status``."""
    _sync_toolchain_from_api(api)
    clear_toolchain_cache()
    scan = scan_toolchain()
    status = store.record_toolchain_scan(str(job_id), scan)
    guidance = ""
    missing = list(scan.get("missing") or [])
    if "jadx" in missing:
        if "apktool" in (scan.get("available") or []):
            guidance = (
                "jadx is missing — do NOT call android_jadx_decompile. "
                "Use android_apktool_decode for smali/resources, or ask the user to install JADX."
            )
        else:
            guidance = (
                "Neither jadx nor apktool is available — skip decompile tools until the user "
                "installs them or sets settings.toolchain.paths."
            )
    return {
        "toolchain_status": status,
        "toolchain_available": list(scan.get("available") or []),
        "toolchain_missing": missing,
        "decompile_guidance": guidance,
    }


def _resolve_apk_sample(
    api: Any,
    kwargs: dict[str, Any],
    *,
    path: str,
    job_id: str,
    artifact_id: str,
) -> tuple[Path, dict[str, Any] | None]:
    if artifact_id and job_id:
        store = _resolve_reverse_store(api)
        if store is None:
            raise RuntimeError("reverse-job store unavailable")
        sample, art = _artifact_path(store, job_id, artifact_id)
        return sample, art
    sample = _resolve_sample_path(api, kwargs, path)
    return sample, None


def _job_dir(store: Any, job_id: str) -> Path:
    job = store.get(job_id) or {}
    job_dir = Path(str(job.get("_dir") or ""))
    if not job_dir.is_dir():
        job_dir = Path(store.root) / job_id
    return job_dir


def _decompile_output_dir(store: Any, job_id: str, tool: str, sample: Path) -> Path:
    digest = sha256_file(sample)[:12]
    job_dir = _job_dir(store, job_id)
    out = job_dir / "decompiled" / f"{tool}-{digest}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _resolve_apktool_project_dir(store: Any, job_id: str, project_dir: str) -> Path:
    text = str(project_dir or "").strip()
    if text:
        root = Path(text).expanduser().resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"project_dir not found: {root}")
        return root
    if not job_id:
        raise ValueError("job_id or project_dir is required")
    job = store.get(job_id) or {}
    decompile = (job.get("analysis_summary") or {}).get("static", {}).get("decompile", {})
    apktool = decompile.get("android_apktool_decode") or {}
    out_text = str(apktool.get("output_dir") or "").strip()
    if out_text:
        out = Path(out_text).expanduser().resolve()
        if out.is_dir():
            return out
    job_dir = _job_dir(store, job_id)
    decompiled = job_dir / "decompiled"
    if decompiled.is_dir():
        candidates = sorted(
            (p for p in decompiled.glob("apktool-*") if p.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            if (candidate / "AndroidManifest.xml").is_file():
                return candidate.resolve()
    raise FileNotFoundError(
        f"no apktool project under job {job_id}; run android_apktool_decode first or pass project_dir="
    )


def _built_apk_path(store: Any, job_id: str, filename: str) -> Path:
    out = _job_dir(store, job_id) / "built"
    out.mkdir(parents=True, exist_ok=True)
    return out / filename


def _result_attachable(result: dict[str, Any]) -> bool:
    return bool(result.get("ok") or result.get("partial"))


def _attach_decompile_to_job(
    store: Any,
    job_id: str,
    result: dict[str, Any],
    *,
    tool: str,
    sample: Path,
    output_dir: Path,
) -> dict[str, Any]:
    if not job_id or not _result_attachable(result):
        return {}
    attached: dict[str, Any] = {}
    summary = {
        "ok": bool(result.get("ok")),
        "partial": bool(result.get("partial")),
        "usable_output": bool(result.get("usable_output")),
        "tool": tool,
        "input": str(sample.resolve()),
        "output_dir": str(output_dir.resolve()),
        "output_dir_rel": output_dir.name,
        "tree": result.get("tree") or {},
        "command": result.get("command"),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "error": result.get("error") or "",
    }
    payload = json.dumps(summary, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    rec = store.add_artifact(
        job_id,
        content=payload,
        kind="trace",
        filename=f"decompile-{tool}.json",
        tool=tool,
        note=f"{tool} decompile index",
    )
    attached["index_artifact"] = rec
    attached["index_artifact_ref"] = artifact_ref(job_id, rec["id"])
    attached["job_id"] = job_id
    attached["output_dir"] = str(output_dir.resolve())

    manifest_rel = str((result.get("tree") or {}).get("manifest_rel") or "")
    if manifest_rel:
        manifest_path = output_dir / manifest_rel
        if manifest_path.is_file() and manifest_path.stat().st_size <= 50 * 1024 * 1024:
            try:
                art_store = ReverseArtifactStore(store)
                man = art_store.register_path(
                    job_id,
                    source_path=manifest_path,
                    kind="manifest",
                    tool=tool,
                    note=f"AndroidManifest from {tool}",
                )
                attached["manifest_artifact"] = man
                attached["manifest_artifact_ref"] = man.get("artifact_ref")
            except Exception:
                pass

    job = store.get(job_id) or {}
    static = dict((job.get("analysis_summary") or {}).get("static") or {})
    tools = list(static.get("tools") or [])
    if tool not in tools:
        tools.append(tool)
    decompile = dict(static.get("decompile") or {})
    decompile[tool] = {
        "output_dir": str(output_dir.resolve()),
        "index_artifact_ref": attached.get("index_artifact_ref"),
        "file_count": (result.get("tree") or {}).get("file_count", 0),
        "finished_at": result.get("finished_at"),
    }
    static.update({
        "done": True,
        "tools": tools,
        "decompile": decompile,
        "updated_at": result.get("finished_at") or __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
    })
    store.update(job_id, analysis_summary={**(job.get("analysis_summary") or {}), "static": static})
    return attached


def _attach_apk_build_to_job(
    store: Any,
    job_id: str,
    result: dict[str, Any],
    *,
    tool: str,
    apk_path: Path,
    project_dir: Path | None = None,
) -> dict[str, Any]:
    if not job_id or not _result_attachable(result):
        return {}
    attached: dict[str, Any] = {}
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
    summary = {
        "ok": bool(result.get("ok")),
        "partial": bool(result.get("partial")),
        "usable_output": bool(result.get("usable_output")),
        "tool": tool,
        "project_dir": str(project_dir.resolve()) if project_dir else "",
        "output_apk": str(apk_path.resolve()),
        "command": result.get("command"),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
        "error": result.get("error") or "",
        "steps": result.get("steps") or [],
        "validation": {
            "ok": validation.get("ok"),
            "installable_hint": validation.get("installable_hint"),
            "errors": validation.get("errors") or [],
            "warnings": validation.get("warnings") or [],
            "suggested_fix": validation.get("suggested_fix") or "",
        }
        if validation
        else {},
    }
    payload = json.dumps(summary, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    rec = store.add_artifact(
        job_id,
        content=payload,
        kind="trace",
        filename=f"build-{tool}.json",
        tool=tool,
        note=f"{tool} build index",
    )
    attached["index_artifact"] = rec
    attached["index_artifact_ref"] = artifact_ref(job_id, rec["id"])

    if apk_path.is_file() and apk_path.stat().st_size <= 200 * 1024 * 1024:
        try:
            art_store = ReverseArtifactStore(store)
            apk_art = art_store.register_path(
                job_id,
                source_path=apk_path,
                kind="apk",
                tool=tool,
                note=f"Built/signed APK from {tool}",
            )
            attached["apk_artifact"] = apk_art
            attached["apk_artifact_ref"] = apk_art.get("artifact_ref")
        except Exception:
            pass

    job = store.get(job_id) or {}
    static = dict((job.get("analysis_summary") or {}).get("static") or {})
    tools = list(static.get("tools") or [])
    if tool not in tools:
        tools.append(tool)
    build = dict(static.get("build") or {})
    build[tool] = {
        "output_apk": str(apk_path.resolve()),
        "index_artifact_ref": attached.get("index_artifact_ref"),
        "apk_artifact_ref": attached.get("apk_artifact_ref"),
        "finished_at": result.get("finished_at"),
        "partial": bool(result.get("partial")),
    }
    static.update({
        "done": True,
        "tools": tools,
        "build": build,
        "updated_at": result.get("finished_at") or __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
    })
    store.update(job_id, analysis_summary={**(job.get("analysis_summary") or {}), "static": static})
    return attached


def reverse_toolchain_scan(
    api: Any,
    job_id: str = "",
    tools: list[str] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Probe installed reverse-engineering tools on PATH (Frida, JADX, adb, capa, …). Optionally persist snapshot into ``ReverseJob.toolchain_status``."""
    _sync_toolchain_from_api(api)
    clear_toolchain_cache()
    scan = scan_toolchain(names=list(tools) if tools else None)
    if job_id:
        store = _resolve_reverse_store(api)
        if store is None:
            return {"ok": False, "error": "reverse-job store unavailable", **scan}
        try:
            status = store.record_toolchain_scan(str(job_id), scan)
            scan["job_id"] = job_id
            scan["toolchain_status"] = status
        except FileNotFoundError as exc:
            return {"ok": False, "error": str(exc), **scan}
    from argusai.core.paths import PathManager
    from argusai.core.reverse.toolchain_install import enrich_toolchain_scan_result

    tools_dir = None
    if api is not None and getattr(api, "settings_mgr", None):
        tools_dir = PathManager(data_root=api.settings_mgr.get().data_root).tools_dir
    return enrich_toolchain_scan_result(scan, data_root=tools_dir)


def reverse_toolchain_install(
    api: Any,
    tool: str = "",
    force: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Install one reverse CLI (jadx, frida, apktool, adb, capa, yara, zipalign, apksigner, objection) into ``data_root/tools/reverse/`` and persist ``settings.toolchain.paths.<tool>``.

    Use after ``reverse_toolchain_scan`` shows the tool missing and the user
    has confirmed the install. The download is **persistent** — it survives
    GUI restarts and is shared across sessions, unlike the session sandbox.
    For jarsigner / class-dump (``can_install=false`` in scan output), do
    **not** call this tool; point the user at ``docs_url``.

    Never use ``run_shell_snippet`` / ``sandbox_pip_install`` / ``brew
    install`` / system ``pip install`` to install reverse CLIs — that
    leaves the binary outside ``settings.toolchain.paths`` and the next
    GUI launch will not find it. Pass ``force=true`` only when retrying a
    genuinely failed install in the same loop.
    """
    from argusai.core.paths import PathManager
    from argusai.core.reverse.toolchain import sync_path_overrides_from_settings
    from argusai.core.reverse.toolchain_install import (
        install_reverse_tool,
        installable_tools,
    )

    key = str(tool or "").strip().lower()
    if not key:
        return {
            "ok": False,
            "error": "tool name required",
            "suggested_fix": f"Pick one of {list(installable_tools())} and call reverse_toolchain_install(tool=...).",
        }
    if key not in installable_tools():
        return {
            "ok": False,
            "error": f"{key} is not a managed-install target (manual only)",
            "do_not_retry": True,
            "suggested_fix": (
                "Run reverse_toolchain_scan to see docs_url for this tool. "
                f"Supported tools: {list(installable_tools())}."
            ),
        }
    if api is None or not getattr(api, "settings_mgr", None):
        return {"ok": False, "error": "settings unavailable"}
    current = api.settings_mgr.get()
    sync_path_overrides_from_settings(current)
    tools_dir = PathManager(data_root=current.data_root).tools_dir
    return install_reverse_tool(
        key,
        settings_mgr=api.settings_mgr,
        data_root=tools_dir,
        force=bool(force),
    )


def reverse_sample_import(
    api: Any,
    path: str = "",
    job_id: str = "",
    title: str = "",
    target_type: str = "",
    platform: str = "",
    analyze: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Import a local APK/IPA/PE sample into a ReverseJob as an artifact and optionally run static triage."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        sample = _resolve_sample_path(api, kwargs, path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    kind = _guess_kind_from_path(sample)
    tt = str(target_type or "").strip().lower()
    plat = str(platform or "").strip().lower()
    if not tt:
        tt = {"apk": "android", "aab": "android", "xapk": "android", "ipa": "ios", "pe": "windows"}.get(kind, "unknown")
    if not plat:
        plat = {"android": "android", "ios": "ios", "windows": "windows"}.get(tt, "unknown")

    if not job_id:
        job_title = str(title or sample.stem or "imported sample")[:200]
        job = store.create(title=job_title, target_type=tt, platform=plat, tags=[kind, tt])
        job_id = str(job["id"])
    else:
        job = store.get(job_id)
        if job is None:
            return {"ok": False, "error": f"job not found: {job_id}"}

    artifacts = ReverseArtifactStore(store)
    try:
        record = artifacts.register_path(
            job_id,
            source_path=sample,
            kind=kind,
            tool="reverse_sample_import",
            note=f"imported from {sample}",
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    out: dict[str, Any] = {
        "ok": True,
        "job_id": job_id,
        "artifact": record,
        "artifact_ref": record.get("artifact_ref"),
        "sha256": sha256_file(sample),
    }
    try:
        out.update(_attach_toolchain_scan(api, store, job_id))
    except Exception:
        pass
    if analyze:
        analysis = _run_static_for_path(sample, kind=kind)
        analysis["analyzed_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
        out["analysis"] = analysis
        if analysis.get("ok"):
            out["attached"] = _attach_analysis_to_job(store, job_id, analysis, tool=f"{kind}_analyze")
    return out


def android_apk_analyze(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Static triage for Android APK/AAB/XAPK: zip layout, manifest strings, permissions, optional aapt badging."""
    try:
        if artifact_id and job_id:
            store = _resolve_reverse_store(api)
            if store is None:
                return {"ok": False, "error": "reverse-job store unavailable"}
            sample, _art = _artifact_path(store, job_id, artifact_id)
        else:
            sample = _resolve_sample_path(api, kwargs, path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    analysis = analyze_apk_path(sample)
    analysis["analyzed_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
    if attach_to_job and job_id and analysis.get("ok"):
        store = _resolve_reverse_store(api)
        if store is not None:
            analysis["attached"] = _attach_analysis_to_job(store, job_id, analysis, tool="android_apk_analyze")
    return analysis


def android_jadx_decompile(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    output_dir: str = "",
    no_res: bool = False,
    deobf: bool = False,
    threads: int = 4,
    timeout_seconds: int = 600,
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Requires JADX installed — call reverse_toolchain_scan first; skip when jadx not in available."""
    blocked = _preflight_cli_tool(api, "jadx")
    if blocked is not None:
        return blocked
    try:
        sample, _art = _resolve_apk_sample(api, kwargs, path=path, job_id=job_id, artifact_id=artifact_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    store = _resolve_reverse_store(api)
    out: Path
    if str(output_dir or "").strip():
        out = Path(output_dir).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
    elif store and job_id:
        out = _decompile_output_dir(store, job_id, "jadx", sample)
    else:
        out = sample.parent / f"{sample.stem}-jadx-out"
        out.mkdir(parents=True, exist_ok=True)

    result = run_jadx_decompile(
        sample,
        out,
        no_res=no_res,
        deobf=deobf,
        threads=threads,
        timeout_seconds=timeout_seconds,
    )
    if attach_to_job and store and job_id and _result_attachable(result):
        result["attached"] = _attach_decompile_to_job(store, job_id, result, tool="android_jadx_decompile", sample=sample, output_dir=out)
    if job_id:
        result["job_id"] = job_id
    return result


def android_apktool_decode(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    output_dir: str = "",
    force: bool = True,
    no_src: bool = False,
    no_res: bool = False,
    timeout_seconds: int = 300,
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Requires APKTool installed — call reverse_toolchain_scan first; skip when apktool not in available."""
    blocked = _preflight_cli_tool(api, "apktool")
    if blocked is not None:
        return blocked
    try:
        sample, _art = _resolve_apk_sample(api, kwargs, path=path, job_id=job_id, artifact_id=artifact_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    store = _resolve_reverse_store(api)
    out: Path
    if str(output_dir or "").strip():
        out = Path(output_dir).expanduser().resolve()
        out.mkdir(parents=True, exist_ok=True)
    elif store and job_id:
        out = _decompile_output_dir(store, job_id, "apktool", sample)
    else:
        out = sample.parent / f"{sample.stem}-apktool-out"
        out.mkdir(parents=True, exist_ok=True)

    result = run_apktool_decode(
        sample,
        out,
        force=force,
        no_src=no_src,
        no_res=no_res,
        timeout_seconds=timeout_seconds,
    )
    if attach_to_job and store and job_id and _result_attachable(result):
        result["attached"] = _attach_decompile_to_job(store, job_id, result, tool="android_apktool_decode", sample=sample, output_dir=out)
    if job_id:
        result["job_id"] = job_id
    return result


def android_apktool_build(
    api: Any,
    job_id: str = "",
    project_dir: str = "",
    output_apk: str = "",
    force: bool = True,
    timeout_seconds: int = 600,
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Requires APKTool — rebuild APK from job decompiled/apktool-* project; output under reverse_jobs/<id>/built/."""
    blocked = _preflight_cli_tool(api, "apktool")
    if blocked is not None:
        return blocked
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    if not job_id:
        return {"ok": False, "error": "job_id is required for android_apktool_build"}
    try:
        project = _resolve_apktool_project_dir(store, job_id, project_dir)
    except (ValueError, FileNotFoundError) as exc:
        return {"ok": False, "error": str(exc)}

    if str(output_apk or "").strip():
        out_apk = Path(output_apk).expanduser().resolve()
    else:
        digest = sha256_file(project / "AndroidManifest.xml")[:12]
        out_apk = _built_apk_path(store, job_id, f"apktool-build-{digest}.apk")

    result = run_apktool_build(
        project,
        out_apk,
        force=force,
        timeout_seconds=timeout_seconds,
    )
    if attach_to_job and _result_attachable(result):
        result["attached"] = _attach_apk_build_to_job(
            store,
            job_id,
            result,
            tool="android_apktool_build",
            apk_path=out_apk,
            project_dir=project,
        )
    result["job_id"] = job_id
    return result


def android_apk_sign(
    api: Any,
    job_id: str = "",
    artifact_id: str = "",
    path: str = "",
    output_apk: str = "",
    keystore_path: str = "",
    keystore_pass: str = "",
    key_alias: str = "",
    key_pass: str = "",
    use_debug_keystore: bool = False,
    skip_zipalign: bool = False,
    timeout_seconds: int = 180,
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Sign an unsigned APK (zipalign optional) with apksigner or jarsigner; prefer job built/ artifacts over sandbox paths."""
    blocked = _preflight_any_cli_tool(api, "apksigner", "jarsigner")
    if blocked is not None:
        return blocked
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        if artifact_id and job_id:
            unsigned, _art = _artifact_path(store, job_id, artifact_id)
        elif str(path or "").strip():
            unsigned = _resolve_sample_path(api, kwargs, path)
        elif job_id:
            build = (store.get(job_id) or {}).get("analysis_summary", {}).get("static", {}).get("build", {})
            built = build.get("android_apktool_build") or {}
            out_text = str(built.get("output_apk") or "").strip()
            if not out_text or not Path(out_text).is_file():
                return {
                    "ok": False,
                    "error": "unsigned apk not found; run android_apktool_build or pass artifact_id/path",
                }
            unsigned = Path(out_text).resolve()
        else:
            return {"ok": False, "error": "job_id with artifact_id, path, or prior android_apktool_build is required"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    if str(output_apk or "").strip():
        signed = Path(output_apk).expanduser().resolve()
    elif job_id:
        signed = _built_apk_path(store, job_id, f"signed-{unsigned.stem}.apk")
    else:
        signed = unsigned.with_name(f"{unsigned.stem}-signed.apk")

    result = run_apk_sign(
        unsigned,
        signed,
        keystore_path=keystore_path,
        keystore_pass=keystore_pass,
        key_alias=key_alias,
        key_pass=key_pass,
        use_debug_keystore=use_debug_keystore,
        skip_zipalign=skip_zipalign,
        timeout_seconds=timeout_seconds,
    )
    if attach_to_job and job_id and _result_attachable(result):
        result["attached"] = _attach_apk_build_to_job(
            store,
            job_id,
            result,
            tool="android_apk_sign",
            apk_path=signed,
        )
    if job_id:
        result["job_id"] = job_id
    return result


def android_apk_validate(
    api: Any,
    job_id: str = "",
    artifact_id: str = "",
    path: str = "",
    require_signature: bool = True,
    timeout_seconds: int = 90,
    **kwargs: Any,
) -> dict[str, Any]:
    """Offline APK installability checks (zip layout, native-lib compression, zipalign, signature)."""
    store = _resolve_reverse_store(api)
    try:
        if artifact_id and job_id:
            if store is None:
                return {"ok": False, "error": "reverse-job store unavailable"}
            apk, _art = _artifact_path(store, job_id, artifact_id)
        elif str(path or "").strip():
            apk = _resolve_sample_path(api, kwargs, path)
        elif job_id and store is not None:
            build = (store.get(job_id) or {}).get("analysis_summary", {}).get("static", {}).get("build", {})
            for key in ("android_apk_sign", "android_apktool_build"):
                out_text = str((build.get(key) or {}).get("output_apk") or "").strip()
                if out_text and Path(out_text).is_file():
                    apk = Path(out_text).resolve()
                    break
            else:
                return {
                    "ok": False,
                    "error": "apk not found; pass artifact_id/path or run android_apktool_build / android_apk_sign first",
                }
        else:
            return {"ok": False, "error": "job_id with artifact_id, path, or prior build/sign output is required"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    result = validate_apk_package(
        apk,
        require_signature=bool(require_signature),
        timeout_seconds=timeout_seconds,
    )
    if job_id:
        result["job_id"] = job_id
    return result


def ios_ipa_analyze(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Static triage for iOS IPA: Payload/Info.plist, bundle id, ATS, URL schemes."""
    try:
        if artifact_id and job_id:
            store = _resolve_reverse_store(api)
            if store is None:
                return {"ok": False, "error": "reverse-job store unavailable"}
            sample, _art = _artifact_path(store, job_id, artifact_id)
        else:
            sample = _resolve_sample_path(api, kwargs, path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    analysis = analyze_ipa_path(sample)
    analysis["analyzed_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
    if attach_to_job and job_id and analysis.get("ok"):
        store = _resolve_reverse_store(api)
        if store is not None:
            analysis["attached"] = _attach_analysis_to_job(store, job_id, analysis, tool="ios_ipa_analyze")
    return analysis


def ios_class_dump(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    output_dir: str = "",
    timeout_seconds: int = 180,
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run class-dump -H on the main IPA binary (macOS + class-dump on PATH only)."""
    blocked = _preflight_cli_tool(api, "class-dump")
    if blocked is not None:
        return blocked
    try:
        if artifact_id and job_id:
            store = _resolve_reverse_store(api)
            if store is None:
                return {"ok": False, "error": "reverse-job store unavailable"}
            sample, _art = _artifact_path(store, job_id, artifact_id)
        else:
            sample = _resolve_sample_path(api, kwargs, path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    store = _resolve_reverse_store(api)
    work_root: Path
    if str(output_dir or "").strip():
        work_root = Path(output_dir).expanduser().resolve()
    elif store and job_id:
        work_root = _job_dir(store, job_id) / "decompiled" / f"class-dump-{sample.stem}"
    else:
        work_root = sample.parent / f"{sample.stem}-class-dump"
    work_root.mkdir(parents=True, exist_ok=True)
    bin_dir = work_root / "_binary"
    extracted = extract_main_binary(sample, bin_dir)
    if not extracted.get("ok"):
        return extracted

    headers_dir = work_root / "headers"
    result = run_class_dump(
        Path(str(extracted["path"])),
        headers_dir,
        timeout_seconds=timeout_seconds,
    )
    result["ipa"] = str(sample.resolve())
    result["main_binary"] = extracted
    result["output_dir"] = str(headers_dir.resolve())
    if attach_to_job and store and job_id and _result_attachable(result):
        index = {
            "tool": "ios_class_dump",
            "output_dir": result["output_dir"],
            "header_count": result.get("header_count", 0),
            "headers_preview": result.get("headers_preview") or [],
            "main_binary": extracted.get("entry"),
        }
        payload = json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8")
        rec = store.add_artifact(
            job_id,
            content=payload,
            kind="decompiled",
            filename=f"class-dump-{sample.stem}.json",
            tool="ios_class_dump",
            note=f"class-dump headers ({result.get('header_count', 0)} files)",
        )
        result["attached"] = {
            "artifact": rec,
            "artifact_ref": artifact_ref(job_id, rec["id"]),
            "index_artifact_ref": artifact_ref(job_id, rec["id"]),
            "output_dir": result["output_dir"],
            "job_id": job_id,
        }
    if job_id:
        result["job_id"] = job_id
    return result


def windows_pe_analyze(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Static triage for Windows PE (EXE/DLL): headers, sections, import DLLs, suspicious APIs."""
    try:
        if artifact_id and job_id:
            store = _resolve_reverse_store(api)
            if store is None:
                return {"ok": False, "error": "reverse-job store unavailable"}
            sample, _art = _artifact_path(store, job_id, artifact_id)
        else:
            sample = _resolve_sample_path(api, kwargs, path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    analysis = analyze_pe_path(sample)
    analysis["analyzed_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
    if attach_to_job and job_id and analysis.get("ok"):
        store = _resolve_reverse_store(api)
        if store is not None:
            analysis["attached"] = _attach_analysis_to_job(store, job_id, analysis, tool="windows_pe_analyze")
    return analysis


def elf_analyze(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Static triage for ELF binaries (SO/ELF): headers, sections, NEEDED libs, strings preview."""
    try:
        if artifact_id and job_id:
            store = _resolve_reverse_store(api)
            if store is None:
                return {"ok": False, "error": "reverse-job store unavailable"}
            sample, _art = _artifact_path(store, job_id, artifact_id)
        else:
            sample = _resolve_sample_path(api, kwargs, path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    analysis = analyze_elf_path(sample)
    analysis["analyzed_at"] = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
    if attach_to_job and job_id and analysis.get("ok"):
        store = _resolve_reverse_store(api)
        if store is not None:
            analysis["attached"] = _attach_analysis_to_job(store, job_id, analysis, tool="elf_analyze")
    return analysis


def _resolve_binary_sample(
    api: Any,
    kwargs: dict[str, Any],
    *,
    path: str,
    job_id: str,
    artifact_id: str,
) -> Path:
    if artifact_id and job_id:
        store = _resolve_reverse_store(api)
        if store is None:
            raise RuntimeError("reverse-job store unavailable")
        sample, _art = _artifact_path(store, job_id, artifact_id)
        return sample
    return _resolve_sample_path(api, kwargs, path)


def _attach_report_to_job(
    store: Any,
    job_id: str,
    result: dict[str, Any],
    *,
    tool: str,
    kind: str,
    filename: str,
) -> dict[str, Any]:
    if not job_id or not result.get("ok"):
        return {}
    payload_obj = result.get("report") if kind == "capa_report" else {
        "matches": result.get("match_lines") or [],
        "match_count": result.get("match_count", 0),
        "rules": result.get("rules"),
        "input": result.get("input"),
    }
    payload = json.dumps(payload_obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")
    rec = store.add_artifact(
        job_id,
        content=payload,
        kind=kind,
        filename=filename,
        tool=tool,
        note=f"{tool} report",
    )
    ref = artifact_ref(job_id, rec["id"])
    job = store.get(job_id) or {}
    static = dict((job.get("analysis_summary") or {}).get("static") or {})
    tools = list(static.get("tools") or [])
    if tool not in tools:
        tools.append(tool)
    store.update(
        job_id,
        analysis_summary={
            **(job.get("analysis_summary") or {}),
            "static": {
                **static,
                "done": True,
                "tools": tools,
                "updated_at": result.get("finished_at") or __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
            },
        },
    )
    return {"artifact": rec, "artifact_ref": ref}


def binary_capa_analyze(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    format_hint: str = "",
    timeout_seconds: int = 180,
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run capa static capability analysis on PE/ELF/APK samples. Returns rule summary + optional capa_report artifact."""
    blocked = _preflight_cli_tool(api, "capa")
    if blocked is not None:
        return blocked
    try:
        sample = _resolve_binary_sample(api, kwargs, path=path, job_id=job_id, artifact_id=artifact_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    result = run_capa_analyze(sample, format_hint=format_hint, timeout_seconds=timeout_seconds)
    store = _resolve_reverse_store(api)
    if attach_to_job and store and job_id and result.get("ok"):
        result["attached"] = _attach_report_to_job(
            store, job_id, result, tool="binary_capa_analyze", kind="capa_report", filename="capa-report.json",
        )
    return result


def binary_yara_scan(
    api: Any,
    path: str = "",
    rules_path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    timeout_seconds: int = 120,
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Scan a sample with yara rules file. Returns match lines + optional yara_match artifact."""
    blocked = _preflight_cli_tool(api, "yara")
    if blocked is not None:
        return blocked
    rules = str(rules_path or "").strip()
    if not rules:
        return {"ok": False, "error": "rules_path is required"}
    try:
        sample = _resolve_binary_sample(api, kwargs, path=path, job_id=job_id, artifact_id=artifact_id)
        rules_file = Path(rules).expanduser().resolve()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    result = run_yara_scan(sample, rules_file, timeout_seconds=timeout_seconds)
    store = _resolve_reverse_store(api)
    if attach_to_job and store and job_id and result.get("ok"):
        result["attached"] = _attach_report_to_job(
            store, job_id, result, tool="binary_yara_scan", kind="yara_match", filename="yara-matches.json",
        )
    return result


def installer_unpack(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    output_dir: str = "",
    password: str = "",
    list_only: bool = False,
    timeout_seconds: int = 180,
    **kwargs: Any,
) -> dict[str, Any]:
    """Unpack an Inno Setup installer (many Windows setup.exe) with innoextract — list or extract its bundled files/app binaries/scripts. Encrypted installers need the install password; without it, do NOT brute-force the overlay — install in a lab VM and Frida-hook the validation in the installed exe."""
    blocked = _preflight_cli_tool(api, "innoextract")
    if blocked is not None:
        return blocked
    try:
        sample = _resolve_binary_sample(api, kwargs, path=path, job_id=job_id, artifact_id=artifact_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    info = detect_tool("innoextract")
    exe = str(info.get("path") or "")
    if not exe:
        return tool_unavailable_error("innoextract", info=info)

    if str(output_dir or "").strip():
        out = Path(_resolve_session_output_path(api, kwargs, output_dir)).resolve()
    else:
        out = (_session_sandbox_root(api, kwargs) / "installer-unpack" / (sample.stem or "installer")).resolve()
    out.mkdir(parents=True, exist_ok=True)

    argv = [exe, "--color", "never", "--list" if list_only else "--extract"]
    if not list_only:
        argv += ["--output-dir", str(out)]
    if str(password or "").strip():
        argv += ["--password", str(password).strip()]
    argv.append(str(sample))

    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_seconds,
            check=False, **quiet_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"innoextract failed: {exc}"}

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    lower = (stderr + "\n" + stdout).lower()

    if proc.returncode != 0:
        encrypted = "encrypt" in lower or "password" in lower
        not_inno = ("not an inno setup" in lower or "no supported" in lower
                    or "is not a supported" in lower or "could not open" in lower)
        if encrypted and not str(password or "").strip():
            hint = (
                "This Inno Setup installer is ENCRYPTED. innoextract needs the install password. "
                "If you don't have it, do NOT brute-force the overlay — the product-key / license "
                "validation almost always lives in the INSTALLED main exe, not the installer. "
                "Install it in a lab VM and Frida-hook the check."
            )
        elif not_inno:
            hint = (
                "This is not an Inno Setup installer. For NSIS use 7-Zip; for InstallShield/MSI use "
                "the matching tool. innoextract only handles Inno Setup."
            )
        else:
            hint = "innoextract could not unpack this installer; consider dynamic analysis (install + hook)."
        return {
            "ok": False,
            "tool": "innoextract",
            "error": (stderr or stdout or "innoextract returned non-zero").strip()[:2000],
            "returncode": proc.returncode,
            "encrypted": bool(encrypted),
            "hint": hint,
        }

    if list_only:
        lines = [ln for ln in stdout.splitlines() if ln.strip()]
        return {
            "ok": True,
            "tool": "innoextract",
            "mode": "list",
            "installer": str(sample),
            "file_count": len(lines),
            "files": lines[:500],
            "files_truncated": len(lines) > 500,
        }

    files: list[str] = []
    total = 0
    for p in sorted(out.rglob("*")):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
            files.append(str(p.relative_to(out)))
    result = {
        "ok": True,
        "tool": "innoextract",
        "mode": "extract",
        "installer": str(sample),
        "output_dir": str(out),
        "file_count": len(files),
        "total_bytes": total,
        "files": files[:500],
        "files_truncated": len(files) > 500,
        "summary_text": f"innoextract · extracted {len(files)} files ({total // 1024} KB) to {out}",
        "next_step": (
            "Inspect extracted files (stat_text_file / grep_text / read_text_file). The app .exe/.dll "
            "inside is the real reverse target — feed it to binary_list_functions / binary_decompile."
        ),
    }
    if job_id:
        result["job_id"] = job_id
    return result


def _attach_text_to_job(
    store: Any,
    job_id: str,
    text: str,
    *,
    tool: str,
    kind: str,
    filename: str,
    note: str,
) -> dict[str, Any]:
    """Attach a text artifact (disasm / pseudocode) to a job's artifacts/ subtree."""
    if not store or not job_id or not text:
        return {}
    rec = store.add_artifact(
        job_id,
        content=text,
        kind=kind,
        filename=filename,
        tool=tool,
        note=note,
    )
    return {"artifact": rec, "artifact_ref": artifact_ref(job_id, rec["id"])}


def binary_list_functions(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    limit: int = 400,
    timeout_seconds: int = 300,
    **kwargs: Any,
) -> dict[str, Any]:
    """List functions in a PE/ELF/Mach-O binary with Ghidra."""
    try:
        sample = _resolve_binary_sample(api, kwargs, path=path, job_id=job_id, artifact_id=artifact_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    result = run_ghidra_list_functions(
        api,
        sample,
        limit=limit,
        timeout_seconds=timeout_seconds,
    )
    if job_id:
        result["job_id"] = job_id
    return result


def binary_disassemble(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    function: str = "",
    address: str = "",
    count: int = 0,
    timeout_seconds: int = 300,
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Disassemble one function or bounded instruction range with Ghidra."""
    try:
        sample = _resolve_binary_sample(api, kwargs, path=path, job_id=job_id, artifact_id=artifact_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    result = run_ghidra_disassemble(
        api,
        sample,
        function=function,
        address=address,
        count=count,
        timeout_seconds=timeout_seconds,
    )
    store = _resolve_reverse_store(api)
    if attach_to_job and store and job_id and result.get("ok") and result.get("disasm"):
        result["attached"] = _attach_text_to_job(
            store, job_id, result["disasm"],
            tool="binary_disassemble", kind="disasm",
            filename=f"disasm-{result.get('target', 'target')}.asm",
            note=f"disasm {result.get('target', '')}",
        )
    if job_id:
        result["job_id"] = job_id
    return result


def binary_decompile(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    function: str = "",
    address: str = "",
    timeout_seconds: int = 300,
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Decompile one function to C-like pseudocode with Ghidra."""
    try:
        sample = _resolve_binary_sample(api, kwargs, path=path, job_id=job_id, artifact_id=artifact_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    result = run_ghidra_decompile(
        api,
        sample,
        function=function,
        address=address,
        timeout_seconds=timeout_seconds,
    )
    store = _resolve_reverse_store(api)
    if attach_to_job and store and job_id and result.get("ok") and result.get("pseudocode"):
        result["attached"] = _attach_text_to_job(
            store, job_id, result["pseudocode"],
            tool="binary_decompile", kind="decompiled",
            filename=f"decompiled-{result.get('target', 'target')}.c",
            note=f"pseudocode {result.get('target', '')} ({result.get('engine', '')})",
        )
    if job_id:
        result["job_id"] = job_id
    return result


def binary_patch(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    patches: Any = None,
    expect_original: bool = True,
    recompute_checksum: bool = True,
    output_name: str = "",
    attach_to_job: bool = True,
    confirm_label: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Patch bytes in a native binary, writing a NEW patched copy (never in place).

    Destructive: requires explicit confirmation (confirm_label=PATCH_BINARY). Each
    patch targets a file ``offset`` or PE ``rva``/``va`` and supplies replacement
    ``bytes`` (hex), ``nop``+``count``, or ``assemble`` (keystone). Provide
    ``expect`` (hex) per patch to assert the original bytes before writing.
    """
    if str(confirm_label or "").strip().upper() != "PATCH_BINARY":
        return {
            "ok": False,
            "needs_confirmation": True,
            "error": "binary_patch modifies an executable. Re-call with confirm_label=PATCH_BINARY after user approval.",
        }
    if not isinstance(patches, list) or not patches:
        return {"ok": False, "error": "patches must be a non-empty array of patch objects"}
    try:
        sample = _resolve_binary_sample(api, kwargs, path=path, job_id=job_id, artifact_id=artifact_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    suffix = sample.suffix or ".bin"
    default_name = f"{sample.stem}.patched{suffix}"
    out_name = str(output_name or "").strip() or default_name
    # Always write to a new file alongside the sample; never overwrite the input.
    out_path = sample.with_name(out_name)
    if out_path.resolve() == sample.resolve():
        out_path = sample.with_name(f"{sample.stem}.patched{suffix}")

    result = apply_binary_patches(
        sample,
        patches,
        out_path,
        expect_original=expect_original,
        recompute_checksum=recompute_checksum,
    )
    if not result.get("ok"):
        return result

    store = _resolve_reverse_store(api)
    if attach_to_job and store and job_id:
        try:
            payload = out_path.read_bytes()
            rec = store.add_artifact(
                job_id,
                content=payload,
                kind="patched",
                filename=out_name,
                tool="binary_patch",
                note=f"patched copy ({result.get('patch_count', 0)} patch(es))",
            )
            result["attached"] = {"artifact": rec, "artifact_ref": artifact_ref(job_id, rec["id"])}
            result["job_id"] = job_id
        except Exception as exc:  # noqa: BLE001 - artifact attach is best-effort
            result["attach_error"] = str(exc)
    return result


def android_objection_run(
    api: Any,
    package: str = "",
    command: str = "",
    gadget: str = "",
    network: bool = False,
    job_id: str = "",
    timeout_seconds: int = 90,
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a one-shot objection command against an Android package (requires device + frida-server)."""
    blocked = _preflight_cli_tool(api, "objection")
    if blocked is not None:
        return blocked
    result = run_objection_command(
        package,
        command,
        gadget=gadget,
        network=network,
        timeout_seconds=timeout_seconds,
    )
    store = _resolve_reverse_store(api)
    if attach_to_job and store and job_id and result.get("ok"):
        payload = json.dumps(
            {"output": result.get("output"), "command": command, "package": package},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        rec = store.add_artifact(
            job_id,
            content=payload,
            kind="trace",
            filename="objection-output.json",
            tool="android_objection_run",
            note=f"objection: {command[:120]}",
        )
        result["attached"] = {"artifact": rec, "artifact_ref": artifact_ref(job_id, rec["id"])}
    return result


def adb_devices(api: Any, timeout_seconds: int = 15, **kwargs: Any) -> dict[str, Any]:
    """List connected Android devices via ``adb devices -l``."""
    blocked = _preflight_cli_tool(api, "adb")
    if blocked is not None:
        return blocked
    return run_adb_devices(timeout_seconds=timeout_seconds)


def adb_install(
    api: Any,
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    serial: str = "",
    replace: bool = True,
    grant_permissions: bool = False,
    timeout_seconds: int = 120,
    **kwargs: Any,
) -> dict[str, Any]:
    """Install an APK on a connected Android device (lab use). Requires confirmation."""
    blocked = _preflight_cli_tool(api, "adb")
    if blocked is not None:
        return blocked
    try:
        apk, _art = _resolve_apk_sample(api, kwargs, path=path, job_id=job_id, artifact_id=artifact_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return run_adb_install(
        apk,
        serial=serial,
        replace=replace,
        grant_permissions=grant_permissions,
        timeout_seconds=timeout_seconds,
    )


def _resolve_adb_local_path(
    api: Any,
    kwargs: dict[str, Any],
    *,
    path: str,
    job_id: str,
    artifact_id: str,
) -> Path:
    if artifact_id and job_id:
        store = _resolve_reverse_store(api)
        if store is None:
            raise RuntimeError("reverse-job store unavailable")
        sample, _art = _artifact_path(store, job_id, artifact_id)
        return sample
    text = str(path or "").strip()
    if not text:
        raise ValueError("path or job_id+artifact_id is required")
    p = Path(text).expanduser()
    if p.is_file():
        return p.resolve()
    return Path(_resolve_session_input_path(api, kwargs, text)).resolve()


def adb_pull(
    api: Any,
    remote_path: str = "",
    local_path: str = "",
    job_id: str = "",
    serial: str = "",
    attach_to_job: bool = True,
    timeout_seconds: int = 120,
    **kwargs: Any,
) -> dict[str, Any]:
    """Pull a file from the device into the ReverseJob or session sandbox."""
    blocked = _preflight_cli_tool(api, "adb")
    if blocked is not None:
        return blocked
    remote = str(remote_path or "").strip()
    if not remote:
        return {"ok": False, "error": "remote_path is required"}
    jid = str(job_id or "").strip()
    dest_text = str(local_path or "").strip()
    if dest_text:
        dest = Path(dest_text).expanduser().resolve()
    elif jid:
        store = _resolve_reverse_store(api)
        if store is None:
            return {"ok": False, "error": "reverse-job store unavailable"}
        name = Path(remote).name or "adb-pull.bin"
        dest = _job_dir(store, jid) / "adb" / name
    else:
        root = Path(_session_sandbox_root(api, kwargs))
        name = Path(remote).name or "adb-pull.bin"
        dest = root / "adb" / name
    result = run_adb_pull(remote, dest, serial=serial, timeout_seconds=timeout_seconds)
    store = _resolve_reverse_store(api)
    if attach_to_job and store and jid and result.get("ok") and dest.is_file():
        size = dest.stat().st_size
        if size <= 10 * 1024 * 1024:
            rec = store.add_artifact(
                jid,
                content=dest.read_bytes(),
                kind="trace",
                filename=dest.name,
                tool="adb_pull",
                note=f"adb pull {remote}",
            )
            result["attached"] = {"artifact": rec, "artifact_ref": artifact_ref(jid, rec["id"])}
        else:
            result["attach_skipped"] = "file larger than 10MB; see local_path"
    return result


def adb_push(
    api: Any,
    remote_path: str = "",
    path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    serial: str = "",
    timeout_seconds: int = 120,
    **kwargs: Any,
) -> dict[str, Any]:
    """Push a local file or job artifact to the device. Requires confirmation."""
    blocked = _preflight_cli_tool(api, "adb")
    if blocked is not None:
        return blocked
    remote = str(remote_path or "").strip()
    if not remote:
        return {"ok": False, "error": "remote_path is required"}
    try:
        src = _resolve_adb_local_path(api, kwargs, path=path, job_id=job_id, artifact_id=artifact_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return run_adb_push(src, remote, serial=serial, timeout_seconds=timeout_seconds)


def adb_logcat(
    api: Any,
    serial: str = "",
    duration_seconds: int = 10,
    filter_spec: str = "",
    max_lines: int = 500,
    job_id: str = "",
    attach_to_job: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Capture Android logcat for a limited duration (or dump when duration_seconds=0)."""
    blocked = _preflight_cli_tool(api, "adb")
    if blocked is not None:
        return blocked
    result = run_adb_logcat(
        serial=serial,
        duration_seconds=duration_seconds,
        filter_spec=filter_spec,
        max_lines=max_lines,
    )
    store = _resolve_reverse_store(api)
    jid = str(job_id or "").strip()
    if attach_to_job and store and jid and result.get("ok"):
        payload = json.dumps(
            {
                "logcat_text": result.get("logcat_text"),
                "line_count": result.get("line_count"),
                "filter": filter_spec,
                "duration_seconds": duration_seconds,
                "serial": serial or "",
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        rec = store.add_artifact(
            jid,
            content=payload,
            kind="trace",
            filename="adb-logcat.json",
            tool="adb_logcat",
            note=f"adb logcat ({result.get('line_count', 0)} lines)",
        )
        result["attached"] = {"artifact": rec, "artifact_ref": artifact_ref(jid, rec["id"])}
    return result


def mobile_frida_recipe_list(api: Any, platform: str = "", **kwargs: Any) -> dict[str, Any]:
    """List pre-baked mobile Frida hook recipes (Android/iOS). Does not inject — use apply + frida_spawn/attach."""
    recipes = list_recipes(platform=platform)
    return {"ok": True, "count": len(recipes), "recipes": recipes}


def mobile_frida_recipe_apply(
    api: Any,
    job_id: str = "",
    recipe_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Install a mobile Frida recipe script into a ReverseJob as frida_script artifact. Run via frida_spawn/attach with confirmation."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    jid = str(job_id or "").strip()
    rid = str(recipe_id or "").strip()
    if not jid:
        return {"ok": False, "error": "job_id is required"}
    if not rid:
        return {"ok": False, "error": "recipe_id is required"}
    recipe = get_recipe(rid)
    if recipe is None:
        return {"ok": False, "error": f"unknown recipe: {rid}"}
    script = str(recipe.get("script") or "")
    rec = store.add_artifact(
        jid,
        content=script.encode("utf-8"),
        kind="frida_script",
        filename=f"recipe-{rid}.js",
        tool="mobile_frida_recipe_apply",
        note=str(recipe.get("title") or rid),
    )
    return {
        "ok": True,
        "recipe_id": rid,
        "recipe": {k: recipe.get(k) for k in ("id", "title", "description", "platform", "mode", "tags", "coverage")},
        "artifact": rec,
        "artifact_ref": artifact_ref(jid, rec["id"]),
        "next_step": {
            "tool": "frida_spawn" if recipe.get("mode") == "spawn" else "frida_attach",
            "requires_confirmation": True,
            "arguments": {"job_id": jid, "artifact_id": rec["id"]},
        },
    }


def frida_list_devices(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List Frida-visible devices via ``frida-ls-devices`` when installed."""
    info = detect_tool("frida-ls-devices")
    if not info.get("available"):
        info = detect_tool("frida")
        if not info.get("available"):
            return {
                "ok": False,
                "error": "frida-ls-devices not found on PATH",
                "suggested_fix": "User must install frida-tools on the host (e.g. pip install frida-tools). argusai does not auto-install; do not run install via sandbox shell.",
                "toolchain": info,
            }
        cmd = [info["path"], "-D"]
        label = "frida -D"
    else:
        cmd = [info["path"]]
        label = "frida-ls-devices"
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, check=False,
            **quiet_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    text = (proc.stdout or proc.stderr or "").strip()
    return {"ok": proc.returncode == 0, "command": label, "devices_text": text[:8000], "toolchain": info}


def frida_list_processes(
    api: Any,
    device: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """List processes on a Frida device via ``frida-ps``."""
    info = detect_tool("frida-ps")
    if not info.get("available"):
        return {"ok": False, "error": "frida-ps not found on PATH", "suggested_fix": "User must install frida-tools on the host. argusai does not auto-install.", "toolchain": info}
    cmd = [info["path"]]
    if device:
        cmd.extend(["-D", str(device)])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=20, check=False,
            **quiet_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    text = (proc.stdout or proc.stderr or "").strip()
    return {"ok": proc.returncode == 0, "processes_text": text[:12000], "device": device or "local", "toolchain": info}


def _run_static_for_path(path: Path, *, kind: str) -> dict[str, Any]:
    if kind in {"apk", "aab", "xapk"}:
        return analyze_apk_path(path)
    if kind == "ipa":
        return analyze_ipa_path(path)
    if kind == "pe":
        return analyze_pe_path(path)
    if kind == "elf":
        return analyze_elf_path(path)
    return {"ok": False, "error": f"unsupported sample kind: {kind}"}


def _resolve_frida_script(
    api: Any,
    kwargs: dict[str, Any],
    *,
    script: str,
    script_path: str,
    job_id: str,
    artifact_id: str,
) -> tuple[str, Path | None]:
    if script.strip():
        return script, None
    if artifact_id and job_id:
        store = _resolve_reverse_store(api)
        if store is None:
            raise RuntimeError("reverse-job store unavailable")
        path, _art = _artifact_path(store, job_id, artifact_id)
        return "", path
    if script_path.strip():
        p = Path(script_path).expanduser()
        if p.is_file():
            return "", p.resolve()
        return "", Path(_resolve_session_input_path(api, kwargs, script_path)).resolve()
    raise ValueError("script, script_path, or job_id+artifact_id is required")


def _finalize_frida_job(
    store: Any,
    job_id: str,
    result: dict[str, Any],
    *,
    tool: str,
    runtime_session_id: str = "",
) -> dict[str, Any]:
    if not store or not job_id:
        return result
    attached: dict[str, Any] = {}
    art = save_log_artifact(
        store,
        job_id,
        str(result.get("log_path") or ""),
        tool=tool,
        session_id=str(result.get("session_id") or ""),
    )
    if art:
        attached["log_artifact"] = art
        attached["log_artifact_ref"] = art.get("artifact_ref")
    if runtime_session_id and not result.get("background"):
        try:
            store.stop_runtime_session(
                job_id,
                runtime_session_id,
                state="stopped" if result.get("ok") else "failed",
                error=str(result.get("error") or ""),
            )
        except Exception:
            pass
    dynamic = dict((store.get(job_id) or {}).get("analysis_summary", {}).get("dynamic") or {})
    dynamic.update({"done": bool(result.get("ok")), "tools": [tool], "updated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S")})
    try:
        job = store.get(job_id) or {}
        store.update(job_id, analysis_summary={**(job.get("analysis_summary") or {}), "dynamic": dynamic})
    except Exception:
        pass
    if attached:
        result["attached"] = attached
    return result


def frida_attach(
    api: Any,
    process_name: str = "",
    pid: int = 0,
    device: str = "",
    script: str = "",
    script_path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    timeout_seconds: int = 30,
    background: bool = False,
    save_log: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Attach Frida to a running process by name or PID. Side-effect: injects into the target process."""
    try:
        script_text, script_file = _resolve_frida_script(
            api, kwargs, script=script, script_path=script_path, job_id=job_id, artifact_id=artifact_id,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if process_name:
        mode, target = "attach_name", process_name
    elif pid:
        mode, target = "attach_pid", str(int(pid))
    else:
        return {"ok": False, "error": "process_name or pid is required"}

    store = _resolve_reverse_store(api)
    rt_id = ""
    if store and job_id:
        try:
            rt = store.start_runtime_session(
                job_id,
                kind="frida",
                target=target,
                tool="frida_attach",
                meta={"device": device, "session_id": ""},
            )
            rt_id = str(rt.get("id") or "")
        except Exception:
            pass

    result = run_frida(
        mode=mode,
        target=target,
        device=device,
        script=script_text,
        script_path=script_file,
        job_id=job_id,
        timeout_seconds=timeout_seconds,
        background=background,
    )
    if save_log and store and job_id:
        _finalize_frida_job(store, job_id, result, tool="frida_attach", runtime_session_id=rt_id)
    elif store and job_id and rt_id and not result.get("background"):
        try:
            store.stop_runtime_session(job_id, rt_id, state="stopped" if result.get("ok") else "failed", error=str(result.get("error") or ""))
        except Exception:
            pass
    return result


def frida_spawn(
    api: Any,
    package: str = "",
    device: str = "",
    script: str = "",
    script_path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    timeout_seconds: int = 45,
    background: bool = False,
    save_log: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Spawn an app under Frida (``frida -f package``). Starts or restarts the target app."""
    if not str(package or "").strip():
        return {"ok": False, "error": "package is required"}
    try:
        script_text, script_file = _resolve_frida_script(
            api, kwargs, script=script, script_path=script_path, job_id=job_id, artifact_id=artifact_id,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    store = _resolve_reverse_store(api)
    rt_id = ""
    if store and job_id:
        try:
            rt = store.start_runtime_session(job_id, kind="frida", target=package, tool="frida_spawn", meta={"device": device})
            rt_id = str(rt.get("id") or "")
        except Exception:
            pass

    result = run_frida(
        mode="spawn",
        target=package,
        device=device,
        script=script_text,
        script_path=script_file,
        job_id=job_id,
        timeout_seconds=timeout_seconds,
        background=background,
    )
    if save_log and store and job_id:
        _finalize_frida_job(store, job_id, result, tool="frida_spawn", runtime_session_id=rt_id)
    elif store and job_id and rt_id and not result.get("background"):
        try:
            store.stop_runtime_session(job_id, rt_id, state="stopped" if result.get("ok") else "failed", error=str(result.get("error") or ""))
        except Exception:
            pass
    return result


def frida_run_script(
    api: Any,
    mode: str = "attach_name",
    target: str = "",
    device: str = "",
    script: str = "",
    script_path: str = "",
    job_id: str = "",
    artifact_id: str = "",
    timeout_seconds: int = 60,
    background: bool = False,
    save_log: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a Frida script via attach/spawn/pid. General-purpose dynamic instrumentation entry."""
    if not str(target or "").strip():
        return {"ok": False, "error": "target is required (package, process name, or pid)"}
    try:
        script_text, script_file = _resolve_frida_script(
            api, kwargs, script=script, script_path=script_path, job_id=job_id, artifact_id=artifact_id,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    store = _resolve_reverse_store(api)
    rt_id = ""
    if store and job_id:
        try:
            rt = store.start_runtime_session(job_id, kind="frida", target=target, tool="frida_run_script", meta={"mode": mode, "device": device})
            rt_id = str(rt.get("id") or "")
        except Exception:
            pass

    result = run_frida(
        mode=mode,
        target=target,
        device=device,
        script=script_text,
        script_path=script_file,
        job_id=job_id,
        timeout_seconds=timeout_seconds,
        background=background,
    )
    if save_log and store and job_id:
        _finalize_frida_job(store, job_id, result, tool="frida_run_script", runtime_session_id=rt_id)
    elif store and job_id and rt_id and not result.get("background"):
        try:
            store.stop_runtime_session(job_id, rt_id, state="stopped" if result.get("ok") else "failed", error=str(result.get("error") or ""))
        except Exception:
            pass
    return result


def frida_session_list(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List in-process Frida CLI sessions started with ``background=true``."""
    sessions = get_frida_manager().list_sessions()
    return {"ok": True, "count": len(sessions), "sessions": sessions}


# ---------------------------------------------------------------------------
# Filesystem bridging — let the AI hand control of an artifact / job back
# to the user (reveal in OS file manager, copy out, zip the whole job).
#
# These do not modify the job itself; they only surface paths and copy
# bytes. They are low-risk and run without confirmation cards, but they
# DO touch the user's local filesystem outside the session sandbox so we
# keep containment strict — the path resolution always goes through
# ``resolve_artifact_path`` / ``export_reverse_job`` which enforce the
# job-directory boundary.
# ---------------------------------------------------------------------------


def reverse_job_open_dir(api: Any, job_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Open the job's directory in the OS file manager (Explorer/Finder/xdg-open).

    Use this when the user wants to see the raw files for a reverse job
    on disk — the same folder shown by the "Open task folder" button in
    the GUI. Returns ``{ok, path, job_id}`` on success.
    """
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    jid = str(job_id or "").strip()
    if not jid:
        return {"ok": False, "error": "job_id is required"}
    job = store.get(jid, include_trashed=True)
    if job is None:
        return {"ok": False, "error": f"job not found: {jid}"}
    job_dir = Path(str(job.get("_dir") or "")).resolve()
    if not job_dir.is_dir():
        return {"ok": False, "error": f"job directory missing: {job_dir}"}
    result = reveal_path(job_dir)
    result.setdefault("job_id", jid)
    return result


def reverse_artifact_reveal(
    api: Any,
    job_id: str = "",
    artifact_id: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Reveal a single artifact file in the OS file manager (highlighted).

    Resolves the artifact against its job directory (containment-checked)
    and asks the OS to open the containing folder with the file
    selected. Use this when the user wants to inspect a specific
    artifact outside the WebUI preview.
    """
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        target = resolve_artifact_path(store, str(job_id or ""), str(artifact_id or ""))
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    result = reveal_path(target)
    result.setdefault("job_id", str(job_id or ""))
    result.setdefault("artifact_id", str(artifact_id or ""))
    return result


def reverse_artifact_copy_to(
    api: Any,
    job_id: str = "",
    artifact_id: str = "",
    dest_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Copy a single artifact out of the job to a path the user can pick up.

    ``dest_path``:
      * empty (default) → copy into the current assistant session
        sandbox under ``imports/<original-filename>``, so subsequent
        ``export_workspace_bundle`` picks it up.
      * existing directory → append the artifact's filename inside it.
      * any other absolute path → use verbatim (parent created on demand).

    Returns ``{ok, path, size, source, job_id, artifact_id}``. Use this
    when the user wants the actual file (not just a preview), e.g. to
    share a Frida log or attach a capa report to a ticket.
    """
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    try:
        src = resolve_artifact_path(store, str(job_id or ""), str(artifact_id or ""))
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    raw = str(dest_path or "").strip()
    try:
        if raw:
            dest = Path(raw).expanduser().resolve()
            target = dest / src.name if dest.is_dir() else dest
        else:
            session_sandbox = Path(_session_sandbox_root(api, kwargs)).resolve()
            target = (session_sandbox / "imports" / src.name).resolve()
            if session_sandbox not in target.parents:
                return {"ok": False, "error": "imports path escapes session sandbox"}
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        return {
            "ok": True,
            "job_id": str(job_id or ""),
            "artifact_id": str(artifact_id or ""),
            "source": str(src),
            "path": str(target),
            "size": target.stat().st_size,
        }
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


_DECOMPILE_INDEX_PREFIX = "decompile-"
_TEXT_SCAN_SUFFIXES = frozenset({
    ".smali", ".xml", ".java", ".kt", ".kts", ".properties", ".json",
    ".txt", ".yaml", ".yml", ".gradle", ".pro", ".cfg", ".ini", ".html",
    ".js", ".ts", ".csv",
})
_SKIP_SCAN_DIRS = frozenset({
    ".git", ".hg", ".svn", ".idea", ".vscode", ".cursor", "node_modules",
    ".venv", "venv", "__pycache__", "dist", "build", ".cache", "vendor",
})


def _decompile_tree_root(index_path: Path) -> Path | None:
    """Return ``output_dir`` when ``index_path`` is a decompile index JSON."""
    if not index_path.is_file():
        return None
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    tool = str(data.get("tool") or "")
    name_ok = index_path.name.startswith(_DECOMPILE_INDEX_PREFIX)
    tool_ok = tool in {"android_jadx_decompile", "android_apktool_decode"}
    if not (name_ok or tool_ok):
        return None
    out = Path(str(data.get("output_dir") or "")).expanduser().resolve()
    return out if out.is_dir() else None


def _iter_scan_files(root: Path, *, glob: str = "", max_files: int = 5000) -> list[Path]:
    glob_pat = str(glob or "").strip()
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_SCAN_DIRS]
        for name in filenames:
            path = Path(dirpath) / name
            if glob_pat:
                rel = path.relative_to(root).as_posix()
                if not fnmatch.fnmatch(rel, glob_pat) and not fnmatch.fnmatch(name, glob_pat):
                    continue
            elif path.suffix.lower() not in _TEXT_SCAN_SUFFIXES:
                continue
            found.append(path.resolve())
            if len(found) >= max_files:
                return found
    return found


def _extract_strings_scan(
    target: Path,
    *,
    kind: str,
    min_len: Any,
    max_results: Any,
    min_entropy: Any,
    glob: str = "",
) -> dict[str, Any]:
    from argusai.core.infra import text_io

    if target.is_file():
        return text_io.extract_strings(
            path=str(target),
            min_len=text_io.DEFAULT_STRING_MIN_LEN if min_len is None else min_len,
            max_results=text_io.DEFAULT_STRING_RESULTS if max_results is None else max_results,
            kind=str(kind or "any"),
            min_entropy=min_entropy,
            base=None,
        )
    if not target.is_dir():
        return {"ok": False, "error": f"not a file or directory: {target}"}

    try:
        cap = int(text_io.DEFAULT_STRING_RESULTS if max_results is None else max_results)
    except (TypeError, ValueError):
        cap = text_io.DEFAULT_STRING_RESULTS
    cap = max(1, min(cap, text_io.MAX_STRING_RESULTS))
    items: list[dict[str, Any]] = []
    files_scanned = 0
    more = False
    for path in _iter_scan_files(target, glob=glob):
        files_scanned += 1
        remaining = cap - len(items)
        if remaining <= 0:
            more = True
            break
        chunk = text_io.extract_strings(
            path=str(path),
            min_len=text_io.DEFAULT_STRING_MIN_LEN if min_len is None else min_len,
            max_results=remaining,
            kind=str(kind or "any"),
            min_entropy=min_entropy,
            base=None,
        )
        if not chunk.get("ok"):
            continue
        if chunk.get("more"):
            more = True
        for item in chunk.get("items") or []:
            row = dict(item)
            try:
                row["file"] = str(path.relative_to(target))
            except ValueError:
                row["file"] = str(path)
            items.append(row)
            if len(items) >= cap:
                more = True
                break
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "more": more,
        "files_scanned": files_scanned,
        "scan_root": str(target.resolve()),
    }


def reverse_artifact_extract_strings(
    api: Any,
    job_id: str = "",
    artifact_id: str = "",
    kind: str = "any",
    min_len: Any = None,
    max_results: Any = None,
    min_entropy: Any = None,
    relative_path: str = "",
    glob: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Extract URLs/tokens/string literals from a reverse job artifact.

    Requires ``job_id`` + ``artifact_id``. When the artifact is a decompile
    index JSON (JADX/APKTool), scans the indexed ``output_dir`` tree.
    Optional ``relative_path`` narrows to one file inside that tree.
    """
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    jid = str(job_id or "").strip()
    aid = str(artifact_id or "").strip()
    if not jid or not aid:
        return {"ok": False, "error": "job_id and artifact_id are required"}
    try:
        artifact_path = resolve_artifact_path(store, jid, aid)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    decompile_root = _decompile_tree_root(artifact_path)
    rel = str(relative_path or "").strip().replace("\\", "/").lstrip("/")
    if decompile_root is not None:
        scan_target = (decompile_root / rel).resolve() if rel else decompile_root.resolve()
        if not str(scan_target).startswith(str(decompile_root.resolve())):
            return {"ok": False, "error": "relative_path escapes decompile output_dir"}
    elif rel:
        job = store.get(jid)
        if job is None:
            return {"ok": False, "error": f"job not found: {jid}"}
        job_dir = Path(str(job.get("_dir") or "")).resolve()
        scan_target = (job_dir / rel).resolve()
        if not str(scan_target).startswith(str(job_dir)):
            return {"ok": False, "error": "relative_path escapes job directory"}
    else:
        scan_target = artifact_path.resolve()

    if not scan_target.exists():
        return {"ok": False, "error": f"scan target missing: {scan_target}"}

    result = _extract_strings_scan(
        scan_target,
        kind=kind,
        min_len=min_len,
        max_results=max_results,
        min_entropy=min_entropy,
        glob=glob,
    )
    result["job_id"] = jid
    result["artifact_id"] = aid
    if decompile_root is not None:
        result["decompile_output_dir"] = str(decompile_root.resolve())
    return result


def _resolve_reverse_grep_target(
    store: Any,
    job_id: str,
    *,
    artifact_id: str = "",
    relative_path: str = "",
) -> tuple[Path, bool, Path | None]:
    """Return ``(scan_target, is_dir, decompile_root)`` inside a ReverseJob."""
    jid = str(job_id or "").strip()
    aid = str(artifact_id or "").strip()
    rel = str(relative_path or "").strip().replace("\\", "/").lstrip("/")
    job_dir = _job_dir(store, jid).resolve()
    decompile_root: Path | None = None

    if aid:
        artifact_path = resolve_artifact_path(store, jid, aid)
        decompile_root = _decompile_tree_root(artifact_path)
        if decompile_root is not None:
            base = decompile_root.resolve()
        elif artifact_path.is_file():
            if rel:
                target = (artifact_path.parent / rel).resolve()
                if not str(target).startswith(str(job_dir)):
                    raise ValueError("relative_path escapes job directory")
                return target, target.is_dir(), None
            return artifact_path.resolve(), False, None
        else:
            base = artifact_path.resolve()
    else:
        base = (job_dir / "decompiled").resolve()
        if not base.is_dir():
            raise FileNotFoundError(
                "decompiled/ not found — run android_jadx_decompile or android_apktool_decode first, "
                "or pass artifact_id for a decompile index JSON."
            )

    target = (base / rel).resolve() if rel else base
    if not str(target).startswith(str(job_dir)):
        raise ValueError("relative_path escapes job directory")
    if not target.exists():
        raise FileNotFoundError(f"scan target missing: {target}")
    return target, target.is_dir(), decompile_root


def reverse_artifact_grep(
    api: Any,
    job_id: str = "",
    pattern: str = "",
    artifact_id: str = "",
    relative_path: str = "",
    glob: str = "",
    mode: str = "content",
    max_matches: Any = None,
    max_files: Any = None,
    line_trunc: Any = None,
    case_insensitive: Any = False,
    regex: Any = True,
    exclude_dirs: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Grep inside a ReverseJob decompile tree (or a single job artifact file).

    Resolves ``decompiled/`` or a decompile index artifact's ``output_dir`` so
    the Agent does not need to guess absolute paths. Wraps ``grep_text_dir`` /
    ``grep_text_file`` with job containment checks.
    """
    from argusai.core.infra import text_io

    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    jid = str(job_id or "").strip()
    if not jid:
        return {"ok": False, "error": "job_id is required"}
    if not str(pattern or "").strip():
        return {"ok": False, "error": "pattern is required"}
    if store.get(jid) is None:
        return {"ok": False, "error": f"job not found: {jid}"}
    try:
        target, is_dir, decompile_root = _resolve_reverse_grep_target(
            store,
            jid,
            artifact_id=str(artifact_id or ""),
            relative_path=relative_path,
        )
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    job_dir = str(_job_dir(store, jid).resolve())
    if is_dir:
        result = text_io.grep_text_dir(
            pattern=str(pattern or ""),
            root=str(target),
            glob=glob,
            mode=mode,
            max_matches=text_io.DEFAULT_GREP_MATCHES if max_matches is None else max_matches,
            max_files=text_io.MAX_FILES_SCANNED if max_files is None else max_files,
            line_trunc=text_io.MAX_GREP_LINE_LEN if line_trunc is None else line_trunc,
            case_insensitive=case_insensitive,
            regex=regex,
            exclude_dirs=exclude_dirs,
            base=job_dir,
        )
    else:
        result = text_io.grep_text_file(
            pattern=str(pattern or ""),
            path=str(target),
            mode=mode,
            max_matches=text_io.DEFAULT_GREP_MATCHES if max_matches is None else max_matches,
            line_trunc=text_io.MAX_GREP_LINE_LEN if line_trunc is None else line_trunc,
            case_insensitive=case_insensitive,
            regex=regex,
            base=job_dir,
        )
    result["job_id"] = jid
    if str(artifact_id or "").strip():
        result["artifact_id"] = str(artifact_id or "").strip()
    result["scan_root"] = str(target.resolve())
    if decompile_root is not None:
        result["decompile_output_dir"] = str(decompile_root.resolve())
    return result


def reverse_job_link_proxy_flow(
    api: Any,
    job_id: str = "",
    flow_id: int = 0,
    note: str = "",
    attach_har: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Link a MITM proxy flow to a ReverseJob as HAR artifact + evidence ref."""
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    jid = str(job_id or "").strip()
    fid = int(flow_id or 0)
    if not jid:
        return {"ok": False, "error": "job_id is required"}
    if fid <= 0:
        return {"ok": False, "error": "flow_id is required"}
    if store.get(jid) is None:
        return {"ok": False, "error": f"job not found: {jid}"}

    getter = getattr(api, "get_proxy_flow_har", None)
    if not callable(getter):
        return {"ok": False, "error": "proxy HAR export unavailable in this runtime"}
    har_result = getter(fid)
    if not har_result.get("ok"):
        return har_result

    summary = har_result.get("flow_summary") or {}
    link_note = str(note or "").strip() or str(summary.get("url") or summary.get("host") or f"flow {fid}")
    evidence = store.link_evidence_ref(
        jid,
        ref_type="proxy_flow",
        ref_id=str(fid),
        note=link_note[:500],
    )

    attached: dict[str, Any] = {}
    if attach_har:
        payload = json.dumps(har_result.get("har") or {}, ensure_ascii=False, indent=2).encode("utf-8")
        rec = store.add_artifact(
            jid,
            content=payload,
            kind="har",
            filename=f"proxy-flow-{fid}.har.json",
            tool="reverse_job_link_proxy_flow",
            note=link_note[:500],
        )
        attached = {"artifact": rec, "artifact_ref": artifact_ref(jid, rec["id"])}

    return {
        "ok": True,
        "job_id": jid,
        "flow_id": fid,
        "flow_summary": summary,
        "evidence_ref": evidence,
        "attached": attached,
        "next_step": "Use har_import_analyze or web_request_replay on URLs from the stored HAR artifact.",
    }


def reverse_job_export(
    api: Any,
    job_id: str = "",
    dest_dir: str = "",
    include_decompiled: bool = False,
    as_zip: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Export the whole reverse job to a folder or zip (artifacts/findings/etc).

    Wraps ``argusai.core.reverse.reverse_job_export``. Defaults bundle
    ``job.json``, ``events.jsonl`` and the
    ``artifacts/hooks/findings/reproduce/oracles`` subtrees but skip
    ``decompiled/`` (potentially hundreds of MB) — pass
    ``include_decompiled=True`` to include it.

    ``dest_dir`` empty → ``<reverse_jobs_root>/_exports/``. Returns
    ``{ok, path, format, files, bytes, job_id}``.
    """
    store = _resolve_reverse_store(api)
    if store is None:
        return {"ok": False, "error": "reverse-job store unavailable"}
    jid = str(job_id or "").strip()
    if not jid:
        return {"ok": False, "error": "job_id is required"}
    try:
        result = export_reverse_job(
            store,
            jid,
            dest_dir=(dest_dir or None),
            include_decompiled=bool(include_decompiled),
            as_zip=bool(as_zip),
        )
        return result
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def frida_session_stop(api: Any, session_id: str = "", job_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Stop a background Frida session started by this app process."""
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": False, "error": "session_id is required"}
    result = get_frida_manager().stop(sid)
    store = _resolve_reverse_store(api)
    if store and job_id:
        job = store.get(job_id)
        if job:
            for sess in job.get("runtime_sessions") or []:
                if isinstance(sess, dict) and (sess.get("meta") or {}).get("session_id") == sid:
                    try:
                        store.stop_runtime_session(job_id, str(sess.get("id") or ""), state="stopped")
                    except Exception:
                        pass
                    break
    return result
