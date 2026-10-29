"""Pure-Python Android APK static triage (+ optional aapt)."""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path
from typing import Any

from argusai.core.reverse.native_common import (
    _PERMISSION_RE,
    extract_ascii_strings,
    extract_utf16le_strings,
    guess_package_names,
    sha256_bytes,
    sha256_file,
    summarize_zip_entries,
)
from argusai.core.infra.win_stdio import quiet_subprocess_kwargs
from argusai.core.reverse.toolchain import detect_tool


def _manifest_strings(blob: bytes) -> list[str]:
    strings = extract_ascii_strings(blob, limit=800)
    strings.extend(extract_utf16le_strings(blob, limit=400))
    return strings


def _categorize_apk_entries(names: list[str]) -> dict[str, Any]:
    dex_files: list[str] = []
    native_libs: list[str] = []
    assets: list[str] = []
    for name in names:
        lower = name.lower()
        if lower.endswith(".dex"):
            dex_files.append(name)
        elif lower.startswith("lib/") or "/lib/" in lower:
            if lower.endswith(".so"):
                native_libs.append(name)
        elif lower.startswith("assets/"):
            assets.append(name)
    return {
        "dex_files": dex_files[:32],
        "dex_count": len(dex_files),
        "native_libs": native_libs[:64],
        "native_lib_count": len(native_libs),
        "assets_preview": assets[:32],
    }


def _run_aapt_badging(path: Path) -> dict[str, Any] | None:
    info = detect_tool("aapt")
    if not info.get("available"):
        return None
    try:
        proc = subprocess.run(
            [info["path"], "dump", "badging", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            **quiet_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (proc.stdout or proc.stderr or "").strip()
    if not text or proc.returncode != 0:
        return {"ok": False, "error": text[:500] or "aapt failed"}
    package = ""
    version = ""
    sdk = ""
    for line in text.splitlines()[:40]:
        if line.startswith("package:"):
            package = line
        elif line.startswith("application-label:"):
            version = line
        elif line.startswith("sdkVersion:"):
            sdk = line
    return {
        "ok": True,
        "raw_preview": text[:4000],
        "package_line": package,
        "label_line": version,
        "sdk_line": sdk,
    }


def analyze_apk_path(path: Path, *, max_manifest_bytes: int = 2_000_000) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    if path.suffix.lower() not in {".apk", ".aab", ".xapk", ".zip"} and path.name.lower() != "base.apk":
        pass  # still try zip parse

    try:
        digest = sha256_file(path)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    result: dict[str, Any] = {
        "ok": True,
        "format": "apk",
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": digest,
        "confidence": "probable",
        "tools_used": ["zip"],
    }

    try:
        with zipfile.ZipFile(path) as zf:
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            result["zip"] = summarize_zip_entries(zf)
            result["layout"] = _categorize_apk_entries(names)
            manifest_name = next((n for n in names if n.lower().endswith("androidmanifest.xml")), "")
            if manifest_name:
                blob = zf.read(manifest_name)[:max_manifest_bytes]
                strings = _manifest_strings(blob)
                perms = sorted({m.group(0) for s in strings for m in [_PERMISSION_RE.search(s)] if m})
                packages = guess_package_names(strings)
                result["manifest"] = {
                    "entry": manifest_name,
                    "size": len(blob),
                    "permissions": perms[:128],
                    "permission_count": len(perms),
                    "package_candidates": packages,
                    "strings_preview": strings[:80],
                }
            else:
                result["manifest"] = {"error": "AndroidManifest.xml not found in archive"}
                result["confidence"] = "low"
    except zipfile.BadZipFile:
        return {"ok": False, "error": "not a valid zip/apk archive", "sha256": digest}

    aapt = _run_aapt_badging(path)
    if aapt:
        result["aapt"] = aapt
        result["tools_used"].append("aapt")
        if aapt.get("ok"):
            result["confidence"] = "high"

    findings = _triage_findings(result)
    result["findings"] = findings
    result["risk_tags"] = sorted({f["tag"] for f in findings if f.get("tag")})
    return result


def analyze_apk_bytes(data: bytes, *, filename: str = "sample.apk") -> dict[str, Any]:
    import tempfile

    digest = sha256_bytes(data)
    with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix or ".apk", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        out = analyze_apk_path(tmp_path)
        out["sha256"] = digest
        out["filename"] = filename
        return out
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _triage_findings(result: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    manifest = result.get("manifest") or {}
    perms = list(manifest.get("permissions") or [])
    dangerous = [p for p in perms if any(x in p for x in (
        "READ_SMS", "SEND_SMS", "READ_CONTACTS", "RECORD_AUDIO", "CAMERA",
        "ACCESS_FINE_LOCATION", "WRITE_EXTERNAL_STORAGE", "INSTALL_PACKAGES",
    ))]
    if dangerous:
        out.append({
            "kind": "note",
            "tag": "sensitive_permissions",
            "title": "APK requests sensitive Android permissions",
            "confidence": "probable",
            "details": dangerous[:20],
        })
    if result.get("layout", {}).get("native_lib_count", 0) > 0:
        out.append({
            "kind": "note",
            "tag": "native_code",
            "title": "APK ships native libraries (.so)",
            "confidence": "verified",
            "details": result["layout"].get("native_libs", [])[:10],
        })
    if manifest.get("error"):
        out.append({
            "kind": "note",
            "tag": "manifest_missing",
            "title": "AndroidManifest.xml missing or unreadable",
            "confidence": "verified",
            "details": manifest.get("error"),
        })
    return out
