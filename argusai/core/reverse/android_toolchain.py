"""JADX / APKTool CLI bridges for Android reverse engineering.

Outputs are written under a ReverseJob directory; summaries (not full trees)
are returned to Agent tools. Neither JADX nor APKTool is bundled in vendor/.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from argusai.core.infra.win_stdio import quiet_subprocess_kwargs
from argusai.core.reverse.toolchain import detect_tool

log = logging.getLogger("argusai.android_toolchain")

_PREVIEW_PATHS = 40
_DEFAULT_JADX_TIMEOUT = 600
_DEFAULT_APKTOOL_TIMEOUT = 300
_DEFAULT_APKTOOL_BUILD_TIMEOUT = 600
_DEFAULT_APK_SIGN_TIMEOUT = 180
_DEFAULT_APK_VALIDATE_TIMEOUT = 90
_DEBUG_KEYSTORE_ALIAS = "androiddebugkey"
_DEBUG_KEYSTORE_PASS = "android"
_NATIVE_LIB_RE = re.compile(r"^lib/[^/]+/[^/]+\.so$")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _tool_executable(name: str) -> tuple[str | None, dict[str, Any]]:
    info = detect_tool(name)
    path = str(info.get("path") or "").strip()
    if info.get("available") and path:
        return path, info
    return None, info


def _terminate_process_tree(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                **quiet_subprocess_kwargs(),
            )
        else:
            import os
            import signal

            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                proc.kill()
            except AttributeError:
                proc.kill()
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def _run_cli(
    cmd: list[str],
    *,
    timeout_seconds: int,
    cwd: Path | None = None,
) -> dict[str, Any]:
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd) if cwd else None,
            start_new_session=sys.platform != "win32",
            **quiet_subprocess_kwargs(),
        )
    except OSError as exc:
        return {
            "ok": False,
            "command": " ".join(cmd),
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
            "error": str(exc),
            "timed_out": False,
        }
    try:
        stdout, stderr = proc.communicate(timeout=max(1, int(timeout_seconds)))
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
        stdout, stderr = proc.communicate(timeout=5)
        msg = f"command timed out after {timeout_seconds} seconds"
        return {
            "ok": False,
            "command": " ".join(cmd),
            "returncode": -1,
            "stdout": (stdout or "").strip()[:8000],
            "stderr": (stderr or "").strip()[:8000],
            "error": msg,
            "timed_out": True,
            "retryable": False,
        }
    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    ok = proc.returncode == 0
    return {
        "ok": ok,
        "command": " ".join(cmd),
        "returncode": proc.returncode,
        "stdout": stdout[:8000],
        "stderr": stderr[:8000],
        "error": "" if ok else (stderr or stdout or f"exit code {proc.returncode}"),
        "timed_out": False,
    }


def _apply_timeout_partial(
    result: dict[str, Any],
    *,
    tool: str,
    tree: dict[str, Any] | None = None,
    output_file: Path | None = None,
    min_files: int = 1,
    require_manifest: bool = False,
) -> dict[str, Any]:
    if result.get("ok") or not result.get("timed_out"):
        return result
    out = dict(result)
    file_count = int((tree or {}).get("file_count") or 0)
    manifest = bool((tree or {}).get("manifest_rel"))
    file_ok = output_file.is_file() and output_file.stat().st_size > 0 if output_file else False
    usable = file_ok or (file_count >= min_files and (not require_manifest or manifest))
    if not usable:
        return out
    out["partial"] = True
    out["usable_output"] = True
    out["retryable"] = False
    if file_ok:
        out["suggested_fix"] = (
            f"{tool} timed out but output file exists ({output_file}). "
            "Continue with signing/install or re-run with a higher timeout_seconds."
        )
    else:
        out["suggested_fix"] = (
            f"{tool} timed out but output_dir already has {file_count} files"
            + (" and AndroidManifest.xml" if manifest else "")
            + ". Continue static analysis or re-run with a higher timeout_seconds."
        )
    return out


def summarize_output_tree(root: Path, *, preview_limit: int = _PREVIEW_PATHS) -> dict[str, Any]:
    if not root.exists():
        return {"ok": False, "error": f"output dir missing: {root}"}
    file_count = 0
    total_bytes = 0
    ext_counts: Counter[str] = Counter()
    preview: list[str] = []
    manifest_path = ""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        file_count += 1
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
        ext = path.suffix.lower() or "(noext)"
        ext_counts[ext] += 1
        if rel.endswith("AndroidManifest.xml") and not manifest_path:
            manifest_path = rel
        if len(preview) < preview_limit:
            preview.append(rel)
    return {
        "ok": True,
        "output_dir": str(root),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "extensions": dict(ext_counts.most_common(20)),
        "manifest_rel": manifest_path,
        "paths_preview": preview,
        "paths_truncated": file_count > preview_limit,
    }


def run_jadx_decompile(
    apk_path: Path,
    output_dir: Path,
    *,
    no_res: bool = False,
    deobf: bool = False,
    threads: int = 4,
    timeout_seconds: int = _DEFAULT_JADX_TIMEOUT,
) -> dict[str, Any]:
    """Decompile APK/DEX with JADX into ``output_dir``."""
    exe, info = _tool_executable("jadx")
    if not exe:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return tool_unavailable_error("jadx", info=info)
    if not apk_path.is_file():
        return {"ok": False, "error": f"apk not found: {apk_path}"}

    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-d", str(output_dir)]
    if no_res:
        cmd.append("--no-res")
    if deobf:
        cmd.append("--deobf")
    if threads and int(threads) > 0:
        cmd.extend(["--threads-count", str(int(threads))])
    cmd.append(str(apk_path))

    started = _now_iso()
    run = _run_cli(cmd, timeout_seconds=timeout_seconds)
    tree = summarize_output_tree(output_dir)
    java_count = tree.get("extensions", {}).get(".java", 0)
    result: dict[str, Any] = {
        **run,
        "tool": "jadx",
        "input": str(apk_path.resolve()),
        "output_dir": str(output_dir.resolve()),
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
        "tree": tree,
        "java_file_count": java_count,
    }
    if run.get("ok") and tree.get("file_count", 0) == 0:
        result["ok"] = False
        result["error"] = result.get("error") or "jadx produced no output files"
    return result


def run_apktool_decode(
    apk_path: Path,
    output_dir: Path,
    *,
    force: bool = True,
    no_src: bool = False,
    no_res: bool = False,
    timeout_seconds: int = _DEFAULT_APKTOOL_TIMEOUT,
) -> dict[str, Any]:
    """Decode APK with APKTool into ``output_dir`` (smali + resources)."""
    exe, info = _tool_executable("apktool")
    if not exe:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return tool_unavailable_error("apktool", info=info)
    if not apk_path.is_file():
        return {"ok": False, "error": f"apk not found: {apk_path}"}

    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "d", str(apk_path), "-o", str(output_dir)]
    if force:
        cmd.append("-f")
    if no_src:
        cmd.append("-s")
    if no_res:
        cmd.append("-r")

    started = _now_iso()
    run = _run_cli(cmd, timeout_seconds=timeout_seconds)
    tree = summarize_output_tree(output_dir)
    smali_count = tree.get("extensions", {}).get(".smali", 0)
    result: dict[str, Any] = {
        **run,
        "tool": "apktool",
        "input": str(apk_path.resolve()),
        "output_dir": str(output_dir.resolve()),
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
        "tree": tree,
        "smali_file_count": smali_count,
    }
    if run.get("ok") and tree.get("file_count", 0) == 0:
        result["ok"] = False
        result["error"] = result.get("error") or "apktool produced no output files"
    return _apply_timeout_partial(result, tool="apktool decode", tree=tree, require_manifest=True)


def run_apktool_build(
    project_dir: Path,
    output_apk: Path,
    *,
    force: bool = True,
    timeout_seconds: int = _DEFAULT_APKTOOL_BUILD_TIMEOUT,
) -> dict[str, Any]:
    """Build an APK from an APKTool project directory."""
    exe, info = _tool_executable("apktool")
    if not exe:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return tool_unavailable_error("apktool", info=info)
    if not project_dir.is_dir():
        return {"ok": False, "error": f"project_dir not found: {project_dir}"}
    manifest = project_dir / "AndroidManifest.xml"
    if not manifest.is_file():
        return {
            "ok": False,
            "error": f"not an apktool project (missing AndroidManifest.xml): {project_dir}",
        }

    output_apk.parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "b", str(project_dir), "-o", str(output_apk)]
    if force:
        cmd.append("-f")

    started = _now_iso()
    run = _run_cli(cmd, timeout_seconds=timeout_seconds, cwd=project_dir)
    result: dict[str, Any] = {
        **run,
        "tool": "apktool",
        "subcommand": "build",
        "project_dir": str(project_dir.resolve()),
        "output_apk": str(output_apk.resolve()),
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
    }
    if run.get("ok") and not (output_apk.is_file() and output_apk.stat().st_size > 0):
        result["ok"] = False
        result["error"] = result.get("error") or "apktool build produced no output apk"
    result = _apply_timeout_partial(result, tool="apktool build", output_file=output_apk)
    if output_apk.is_file() and output_apk.stat().st_size > 0:
        validation = validate_apk_package(
            output_apk,
            require_signature=False,
            timeout_seconds=min(timeout_seconds, _DEFAULT_APK_VALIDATE_TIMEOUT),
        )
        result["validation"] = validation
        if result.get("ok") and not validation.get("ok"):
            result["ok"] = False
            result["partial"] = True
            result["usable_output"] = True
            result["error"] = validation.get("error") or "apk validation failed after build"
            result["suggested_fix"] = validation.get("suggested_fix") or result.get("suggested_fix")
    return result


def _check_apk_zip_layout(apk_path: Path) -> dict[str, Any]:
    """Inspect APK zip entries for manifest + native-lib compression."""
    if not apk_path.is_file():
        return {"ok": False, "error": f"apk not found: {apk_path}"}
    if not zipfile.is_zipfile(apk_path):
        return {"ok": False, "error": f"not a zip/apk: {apk_path}"}

    has_manifest = False
    compressed_libs: list[dict[str, Any]] = []
    stored_libs: list[str] = []
    other_libs: list[dict[str, Any]] = []
    entry_count = 0
    try:
        with zipfile.ZipFile(apk_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                entry_count += 1
                name = info.filename.replace("\\", "/")
                if name == "AndroidManifest.xml":
                    has_manifest = True
                if not name.startswith("lib/") or not name.endswith(".so"):
                    continue
                row = {
                    "path": name,
                    "compress_type": int(info.compress_type),
                    "compress_size": int(info.compress_size),
                    "file_size": int(info.file_size),
                }
                if _NATIVE_LIB_RE.match(name) is None:
                    other_libs.append(row)
                if info.compress_type == zipfile.ZIP_STORED:
                    stored_libs.append(name)
                else:
                    compressed_libs.append(row)
    except zipfile.BadZipFile as exc:
        return {"ok": False, "error": f"corrupt apk zip: {exc}"}

    errors: list[str] = []
    warnings: list[str] = []
    if not has_manifest:
        errors.append("AndroidManifest.xml missing from APK zip")
    if compressed_libs:
        preview = ", ".join(item["path"] for item in compressed_libs[:6])
        more = f" (+{len(compressed_libs) - 6} more)" if len(compressed_libs) > 6 else ""
        errors.append(
            "native libraries are ZIP-compressed (not STORED); "
            f"common cause of INSTALL_FAILED_INVALID_APK / Failed to extract native libraries: {preview}{more}"
        )
    if other_libs:
        warnings.append(f"{len(other_libs)} unexpected lib/*.so path shape(s)")

    ok = not errors
    return {
        "ok": ok,
        "entry_count": entry_count,
        "has_manifest": has_manifest,
        "native_lib_count": len(stored_libs) + len(compressed_libs),
        "stored_lib_count": len(stored_libs),
        "compressed_lib_count": len(compressed_libs),
        "compressed_libs": compressed_libs[:32],
        "stored_libs_preview": stored_libs[:32],
        "errors": errors,
        "warnings": warnings,
    }


def validate_apk_package(
    apk_path: Path,
    *,
    require_signature: bool = True,
    timeout_seconds: int = _DEFAULT_APK_VALIDATE_TIMEOUT,
) -> dict[str, Any]:
    """Offline installability checks: zip layout, native-lib compression, zipalign, signature."""
    started = _now_iso()
    apk = Path(apk_path).expanduser().resolve()
    checks: dict[str, Any] = {}
    errors: list[str] = []
    warnings: list[str] = []

    layout = _check_apk_zip_layout(apk)
    checks["zip_layout"] = layout
    errors.extend(str(e) for e in (layout.get("errors") or []))
    warnings.extend(str(w) for w in (layout.get("warnings") or []))
    if layout.get("error"):
        errors.append(str(layout["error"]))

    zipalign_exe, zipalign_info = _tool_executable("zipalign")
    if zipalign_exe and apk.is_file():
        align_run = _run_cli(
            [zipalign_exe, "-c", "-p", "4", str(apk)],
            timeout_seconds=min(timeout_seconds, 60),
        )
        checks["zipalign"] = {**align_run, "toolchain": zipalign_info}
        if not align_run.get("ok"):
            msg = str(align_run.get("stderr") or align_run.get("error") or "zipalign -c failed").strip()
            warnings.append(f"zipalign check failed: {msg[:400]}")
    else:
        checks["zipalign"] = {"ok": False, "skipped": True, "error": "zipalign not available", "toolchain": zipalign_info}

    apksigner_exe, apksigner_info = _tool_executable("apksigner")
    if require_signature:
        if apksigner_exe and apk.is_file() and zipfile.is_zipfile(apk):
            verify_run = _run_cli(
                [apksigner_exe, "verify", "--verbose", str(apk)],
                timeout_seconds=min(timeout_seconds, 60),
            )
            checks["signature"] = {**verify_run, "toolchain": apksigner_info, "tool": "apksigner"}
            if not verify_run.get("ok"):
                msg = str(verify_run.get("stderr") or verify_run.get("stdout") or verify_run.get("error") or "apksigner verify failed").strip()
                errors.append(f"signature verify failed: {msg[:500]}")
        else:
            # Soft fallback: look for META-INF signing artifacts when apksigner is missing.
            has_sig = False
            if apk.is_file() and zipfile.is_zipfile(apk):
                try:
                    with zipfile.ZipFile(apk, "r") as zf:
                        names = {n.replace("\\", "/") for n in zf.namelist()}
                        has_sig = any(
                            n.startswith("META-INF/") and (n.endswith(".RSA") or n.endswith(".DSA") or n.endswith(".EC") or n.endswith(".SF"))
                            for n in names
                        )
                except zipfile.BadZipFile:
                    has_sig = False
            checks["signature"] = {
                "ok": has_sig,
                "tool": "meta-inf-heuristic" if not apksigner_exe else "apksigner",
                "skipped": not bool(apksigner_exe),
                "toolchain": apksigner_info,
                "error": "" if has_sig else ("apksigner not available and no META-INF signature entries found" if not apksigner_exe else "signature verify skipped"),
            }
            if not has_sig:
                errors.append(checks["signature"]["error"] or "APK signature missing")
            elif not apksigner_exe:
                warnings.append("apksigner unavailable; signature checked via META-INF entries only")
    else:
        checks["signature"] = {"ok": True, "skipped": True, "reason": "require_signature=false (unsigned build output)"}

    aapt_exe, aapt_info = _tool_executable("aapt")
    if aapt_exe and apk.is_file() and zipfile.is_zipfile(apk):
        badging = _run_cli(
            [aapt_exe, "dump", "badging", str(apk)],
            timeout_seconds=min(timeout_seconds, 60),
        )
        checks["aapt_badging"] = {
            "ok": bool(badging.get("ok")),
            "toolchain": aapt_info,
            "stdout_preview": str(badging.get("stdout") or "")[:800],
            "error": badging.get("error") or "",
        }
        if not badging.get("ok"):
            warnings.append("aapt dump badging failed (package may still install; treat as soft signal)")
    else:
        checks["aapt_badging"] = {"ok": False, "skipped": True, "toolchain": aapt_info}

    ok = not errors
    installable = "likely" if ok and not warnings else ("unlikely" if errors else "uncertain")
    suggested = ""
    if any("native libraries are ZIP-compressed" in e for e in errors):
        suggested = (
            "Re-pack native libs as ZIP_STORED (uncompressed). "
            "apktool rebuilds often deflate lib/**/*.so; fix before sign/install, "
            "or set extractNativeLibs=true in the decoded AndroidManifest application tag and rebuild."
        )
    elif any(e.startswith("signature verify failed") or "signature missing" in e for e in errors):
        suggested = "Re-run android_apk_sign (prefer apksigner + use_debug_keystore for lab installs)."
    elif not ok:
        suggested = "Fix validation errors before adb_install."

    return {
        "ok": ok,
        "apk": str(apk),
        "require_signature": require_signature,
        "installable_hint": installable,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "error": "; ".join(errors) if errors else "",
        "suggested_fix": suggested,
        "started_at": started,
        "finished_at": _now_iso(),
    }


def _default_debug_keystore() -> Path | None:
    ks = Path.home() / ".android" / "debug.keystore"
    return ks if ks.is_file() else None


def _resolve_signing_keystore(
    *,
    keystore_path: str,
    keystore_pass: str,
    key_alias: str,
    key_pass: str,
    use_debug_keystore: bool,
) -> tuple[Path | None, str, str, str, str]:
    ks_text = str(keystore_path or "").strip()
    if ks_text:
        ks = Path(ks_text).expanduser().resolve()
        if not ks.is_file():
            return None, "", "", "", f"keystore not found: {ks}"
        alias = str(key_alias or _DEBUG_KEYSTORE_ALIAS).strip() or _DEBUG_KEYSTORE_ALIAS
        kpass = str(key_pass or keystore_pass or _DEBUG_KEYSTORE_PASS).strip() or _DEBUG_KEYSTORE_PASS
        spass = str(keystore_pass or kpass).strip() or kpass
        return ks, spass, alias, kpass, ""
    if use_debug_keystore:
        ks = _default_debug_keystore()
        if ks is None:
            return (
                None,
                "",
                "",
                "",
                "debug keystore not found (~/.android/debug.keystore); set keystore_path or use_debug_keystore=false with explicit keystore",
            )
        return ks, _DEBUG_KEYSTORE_PASS, _DEBUG_KEYSTORE_ALIAS, _DEBUG_KEYSTORE_PASS, ""
    return None, "", "", "", "keystore_path is required unless use_debug_keystore=true"


def run_apk_sign(
    apk_path: Path,
    output_apk: Path,
    *,
    keystore_path: str = "",
    keystore_pass: str = "",
    key_alias: str = "",
    key_pass: str = "",
    use_debug_keystore: bool = False,
    skip_zipalign: bool = False,
    timeout_seconds: int = _DEFAULT_APK_SIGN_TIMEOUT,
) -> dict[str, Any]:
    """Zipalign (optional) and sign an APK with apksigner or jarsigner."""
    if not apk_path.is_file():
        return {"ok": False, "error": f"apk not found: {apk_path}"}

    ks, store_pass, alias, key_password, err = _resolve_signing_keystore(
        keystore_path=keystore_path,
        keystore_pass=keystore_pass,
        key_alias=key_alias,
        key_pass=key_pass,
        use_debug_keystore=use_debug_keystore,
    )
    if ks is None:
        return {"ok": False, "error": err, "suggested_fix": "Provide keystore_path or use_debug_keystore=true for the Android debug keystore."}

    output_apk.parent.mkdir(parents=True, exist_ok=True)
    work_apk = apk_path
    steps: list[dict[str, Any]] = []
    aligned_path: Path | None = None

    if not skip_zipalign:
        zip_exe, zip_info = _tool_executable("zipalign")
        if zip_exe:
            aligned_path = output_apk.parent / f".align-{output_apk.stem}.apk"
            align_cmd = [zip_exe, "-f", "-p", "4", str(work_apk), str(aligned_path)]
            align_run = _run_cli(align_cmd, timeout_seconds=min(timeout_seconds, 120))
            steps.append({"step": "zipalign", **align_run, "toolchain": zip_info})
            if align_run.get("ok") and aligned_path.is_file():
                work_apk = aligned_path
            else:
                log.warning("zipalign failed; signing unsigned-aligned apk: %s", align_run.get("error"))
        else:
            steps.append({"step": "zipalign", "ok": False, "skipped": True, "error": "zipalign not available", "toolchain": zip_info})

    apksigner_exe, apksigner_info = _tool_executable("apksigner")
    started = _now_iso()
    if apksigner_exe:
        sign_cmd = [
            apksigner_exe,
            "sign",
            "--ks",
            str(ks),
            "--ks-pass",
            f"pass:{store_pass}",
            "--ks-key-alias",
            alias,
            "--key-pass",
            f"pass:{key_password}",
            "--out",
            str(output_apk),
            str(work_apk),
        ]
        sign_run = _run_cli(sign_cmd, timeout_seconds=timeout_seconds)
        steps.append({"step": "apksigner", **sign_run, "toolchain": apksigner_info})
    else:
        jarsigner_exe, jarsigner_info = _tool_executable("jarsigner")
        if not jarsigner_exe:
            return {
                "ok": False,
                "error": "neither apksigner nor jarsigner is available",
                "suggested_fix": "Install Android build-tools (apksigner) or JDK jarsigner, or set settings.toolchain.paths.apksigner",
                "steps": steps,
            }
        unsigned_copy = output_apk.parent / f".unsigned-{output_apk.name}"
        __import__("shutil").copy2(work_apk, unsigned_copy)
        sign_cmd = [
            jarsigner_exe,
            "-sigalg",
            "SHA256withRSA",
            "-digestalg",
            "SHA-256",
            "-keystore",
            str(ks),
            "-storepass",
            store_pass,
            "-keypass",
            key_password,
            str(unsigned_copy),
            alias,
        ]
        sign_run = _run_cli(sign_cmd, timeout_seconds=timeout_seconds)
        steps.append({"step": "jarsigner", **sign_run, "toolchain": jarsigner_info})
        if sign_run.get("ok"):
            __import__("shutil").copy2(unsigned_copy, output_apk)

    ok = output_apk.is_file() and output_apk.stat().st_size > 0
    last = steps[-1] if steps else {}
    result: dict[str, Any] = {
        "ok": ok,
        "tool": "apksigner" if apksigner_exe else "jarsigner",
        "input": str(apk_path.resolve()),
        "output_apk": str(output_apk.resolve()),
        "started_at": started,
        "finished_at": _now_iso(),
        "steps": steps,
        "command": last.get("command", ""),
        "returncode": last.get("returncode", -1 if not ok else 0),
        "stdout": last.get("stdout", ""),
        "stderr": last.get("stderr", ""),
        "error": "" if ok else (last.get("error") or "apk signing failed"),
        "timed_out": any(bool(s.get("timed_out")) for s in steps),
        "keystore": str(ks),
        "key_alias": alias,
        "used_debug_keystore": bool(use_debug_keystore and not keystore_path),
    }
    if aligned_path and aligned_path.is_file():
        try:
            aligned_path.unlink(missing_ok=True)
        except OSError:
            pass
    result = _apply_timeout_partial(result, tool="apk sign", output_file=output_apk)
    if output_apk.is_file() and output_apk.stat().st_size > 0:
        validation = validate_apk_package(
            output_apk,
            require_signature=True,
            timeout_seconds=min(timeout_seconds, _DEFAULT_APK_VALIDATE_TIMEOUT),
        )
        result["validation"] = validation
        if result.get("ok") and not validation.get("ok"):
            result["ok"] = False
            result["partial"] = True
            result["usable_output"] = True
            result["error"] = validation.get("error") or "apk validation failed after sign"
            result["suggested_fix"] = validation.get("suggested_fix") or result.get("suggested_fix")
    return result


def run_objection_command(
    package: str,
    command: str,
    *,
    gadget: str = "",
    network: bool = False,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    """Run a one-shot ``objection -g PACKAGE run "CMD"`` (read-only/mobile triage)."""
    exe, info = _tool_executable("objection")
    if not exe:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return tool_unavailable_error("objection", info=info)
    pkg = str(package or "").strip()
    cmd_text = str(command or "").strip()
    if not pkg:
        return {"ok": False, "error": "package is required"}
    if not cmd_text:
        return {"ok": False, "error": "command is required"}

    target = str(gadget or pkg).strip()
    cmd = [exe, "-g", target, "run", cmd_text]
    if network:
        cmd.insert(1, "-N")

    started = _now_iso()
    run = _run_cli(cmd, timeout_seconds=timeout_seconds)
    return {
        **run,
        "tool": "objection",
        "package": pkg,
        "command": cmd_text,
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
        "output": (run.get("stdout") or run.get("stderr") or "")[:12000],
    }


def _adb_cmd(serial: str = "") -> tuple[list[str] | None, dict[str, Any]]:
    exe, info = _tool_executable("adb")
    if not exe:
        return None, info
    cmd = [exe]
    ser = str(serial or "").strip()
    if ser:
        cmd.extend(["-s", ser])
    return cmd, info


def run_adb_devices(*, timeout_seconds: int = 15) -> dict[str, Any]:
    """Run ``adb devices -l`` and return the device list text."""
    cmd, info = _adb_cmd()
    if cmd is None:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return tool_unavailable_error("adb", info=info)
    started = _now_iso()
    run = _run_cli([*cmd, "devices", "-l"], timeout_seconds=timeout_seconds)
    return {
        **run,
        "tool": "adb",
        "subcommand": "devices",
        "devices_text": run.get("stdout", ""),
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
    }


def run_adb_install(
    apk_path: Path,
    *,
    serial: str = "",
    replace: bool = True,
    grant_permissions: bool = False,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Install an APK on a connected device via ``adb install``."""
    cmd, info = _adb_cmd(serial)
    if cmd is None:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return tool_unavailable_error("adb", info=info)
    apk = Path(apk_path).expanduser().resolve()
    if not apk.is_file():
        return {"ok": False, "error": f"APK not found: {apk}"}
    install_cmd = [*cmd, "install"]
    if replace:
        install_cmd.append("-r")
    if grant_permissions:
        install_cmd.append("-g")
    install_cmd.append(str(apk))
    started = _now_iso()
    run = _run_cli(install_cmd, timeout_seconds=timeout_seconds)
    return {
        **run,
        "tool": "adb",
        "subcommand": "install",
        "apk": str(apk),
        "serial": serial or "",
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
        "output": (run.get("stdout") or run.get("stderr") or "")[:8000],
    }


def run_adb_pull(
    remote_path: str,
    local_path: Path,
    *,
    serial: str = "",
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Pull a file or directory from the device to ``local_path``."""
    cmd, info = _adb_cmd(serial)
    if cmd is None:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return tool_unavailable_error("adb", info=info)
    remote = str(remote_path or "").strip()
    if not remote:
        return {"ok": False, "error": "remote_path is required"}
    dest = Path(local_path).expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    started = _now_iso()
    run = _run_cli([*cmd, "pull", remote, str(dest)], timeout_seconds=timeout_seconds)
    size = dest.stat().st_size if dest.is_file() else 0
    return {
        **run,
        "tool": "adb",
        "subcommand": "pull",
        "remote_path": remote,
        "local_path": str(dest),
        "bytes": size,
        "serial": serial or "",
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
        "output": (run.get("stdout") or run.get("stderr") or "")[:8000],
    }


def run_adb_push(
    local_path: Path,
    remote_path: str,
    *,
    serial: str = "",
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Push a local file to ``remote_path`` on the device."""
    cmd, info = _adb_cmd(serial)
    if cmd is None:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return tool_unavailable_error("adb", info=info)
    src = Path(local_path).expanduser().resolve()
    if not src.is_file():
        return {"ok": False, "error": f"local file not found: {src}"}
    remote = str(remote_path or "").strip()
    if not remote:
        return {"ok": False, "error": "remote_path is required"}
    started = _now_iso()
    run = _run_cli([*cmd, "push", str(src), remote], timeout_seconds=timeout_seconds)
    return {
        **run,
        "tool": "adb",
        "subcommand": "push",
        "local_path": str(src),
        "remote_path": remote,
        "bytes": src.stat().st_size,
        "serial": serial or "",
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
        "output": (run.get("stdout") or run.get("stderr") or "")[:8000],
    }


def run_adb_logcat(
    *,
    serial: str = "",
    duration_seconds: int = 10,
    filter_spec: str = "",
    max_lines: int = 500,
) -> dict[str, Any]:
    """Capture Android logcat with a time limit or one-shot dump."""
    cmd, info = _adb_cmd(serial)
    if cmd is None:
        from argusai.core.reverse.toolchain import tool_unavailable_error

        return tool_unavailable_error("adb", info=info)
    limit = max(1, min(int(max_lines or 500), 5000))
    filt = str(filter_spec or "").strip()
    started = _now_iso()
    if int(duration_seconds or 0) <= 0:
        logcat_cmd = [*cmd, "logcat", "-d", "-t", str(limit), "-v", "time"]
        if filt:
            logcat_cmd.extend(filt.split())
        run = _run_cli(logcat_cmd, timeout_seconds=30)
        mode = "dump"
    else:
        timeout = max(1, min(int(duration_seconds), 300))
        logcat_cmd = [*cmd, "logcat", "-v", "time"]
        if filt:
            logcat_cmd.extend(filt.split())
        run = _run_cli(logcat_cmd, timeout_seconds=timeout)
        mode = "stream"
    text = (run.get("stdout") or "").strip()
    lines = text.splitlines()
    if len(lines) > limit:
        lines = lines[-limit:]
        text = "\n".join(lines)
    return {
        **run,
        "ok": run.get("ok") or bool(text),
        "tool": "adb",
        "subcommand": "logcat",
        "mode": mode,
        "duration_seconds": int(duration_seconds or 0),
        "line_count": len(lines),
        "logcat_text": text[:12000],
        "filter": filt,
        "serial": serial or "",
        "started_at": started,
        "finished_at": _now_iso(),
        "toolchain": info,
    }
