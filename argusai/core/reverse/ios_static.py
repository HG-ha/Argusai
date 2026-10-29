"""Pure-Python iOS IPA static triage."""

from __future__ import annotations

import plistlib
import re
import zipfile
from pathlib import Path
from typing import Any

from argusai.core.reverse.native_common import extract_ascii_strings, sha256_bytes, sha256_file, summarize_zip_entries

_URL_RE = re.compile(r"https?://[^\s\"'<>]{6,}", re.I)
_MACHO_MAGICS = {
    0xFEEDFACE: "macho_32",
    0xCEFAEDFE: "macho_32_swapped",
    0xFEEDFACF: "macho_64",
    0xCFFAEDFE: "macho_64_swapped",
    0xCAFEBABE: "fat",
    0xBEBAFECA: "fat_swapped",
}


def _find_info_plist(names: list[str]) -> str:
    for name in names:
        if name.startswith("Payload/") and name.endswith("/Info.plist"):
            return name
    return next((n for n in names if n.lower().endswith("info.plist")), "")


def _find_app_bundle(names: list[str]) -> str:
    for name in names:
        if name.startswith("Payload/") and ".app/" in name:
            return name.split(".app/")[0] + ".app/"
    for name in names:
        if name.startswith("Payload/") and name.endswith(".app/"):
            return name
    return ""


def _parse_plist(blob: bytes) -> dict[str, Any]:
    if blob[:8] == b"bplist00":
        try:
            data = plistlib.loads(blob)
            return data if isinstance(data, dict) else {"_value": data}
        except Exception as exc:
            return {"_error": f"binary plist parse failed: {exc}"}
    try:
        data = plistlib.loads(blob)
        return data if isinstance(data, dict) else {"_value": data}
    except Exception as exc:
        return {"_error": f"plist parse failed: {exc}"}


def _extract_plist_summary(plist: dict[str, Any]) -> dict[str, Any]:
    bundle_id = str(plist.get("CFBundleIdentifier") or "")
    display = str(plist.get("CFBundleDisplayName") or plist.get("CFBundleName") or "")
    version = str(plist.get("CFBundleShortVersionString") or plist.get("CFBundleVersion") or "")
    min_os = plist.get("MinimumOSVersion") or plist.get("LSMinimumSystemVersion")
    executable = str(plist.get("CFBundleExecutable") or "")
    url_types = plist.get("CFBundleURLTypes") or []
    schemes: list[str] = []
    if isinstance(url_types, list):
        for item in url_types:
            if isinstance(item, dict):
                for s in item.get("CFBundleURLSchemes") or []:
                    schemes.append(str(s))
    ats = plist.get("NSAppTransportSecurity") or {}
    return {
        "bundle_id": bundle_id,
        "display_name": display,
        "version": version,
        "minimum_os": str(min_os or ""),
        "executable": executable,
        "url_schemes": schemes[:32],
        "ats": ats if isinstance(ats, dict) else {},
        "entitlements_preview": _entitlements_keys(plist),
    }


def _entitlements_keys(plist: dict[str, Any]) -> list[str]:
    ent = plist.get("Entitlements")
    if isinstance(ent, dict):
        return sorted(str(k) for k in ent.keys())[:64]
    return []


def _categorize_ipa_entries(names: list[str], app_prefix: str) -> dict[str, Any]:
    frameworks: list[str] = []
    dylibs: list[str] = []
    plists: list[str] = []
    app_binaries: list[str] = []
    mobileprovision = ""
    for name in names:
        lower = name.lower()
        if lower.endswith("embedded.mobileprovision"):
            mobileprovision = name
        if ".framework/" in name:
            fw = name.split(".framework/")[0] + ".framework"
            if fw not in frameworks:
                frameworks.append(fw)
        if lower.endswith(".dylib"):
            dylibs.append(name)
        if lower.endswith(".plist") and not lower.endswith("/info.plist"):
            plists.append(name)
        if app_prefix and name.startswith(app_prefix):
            rel = name[len(app_prefix):]
            if rel and "/" not in rel.rstrip("/") and not rel.endswith("/"):
                app_binaries.append(name)
    return {
        "frameworks": frameworks[:48],
        "framework_count": len(frameworks),
        "dylibs": dylibs[:48],
        "dylib_count": len(dylibs),
        "extra_plists": plists[:32],
        "app_binaries": app_binaries[:16],
        "embedded_mobileprovision": mobileprovision,
    }


def _macho_summary(blob: bytes) -> dict[str, Any]:
    if len(blob) < 4:
        return {"ok": False, "error": "too small for Mach-O"}
    magic_be = int.from_bytes(blob[:4], "big")
    magic_le = int.from_bytes(blob[:4], "little")
    kind = _MACHO_MAGICS.get(magic_be) or _MACHO_MAGICS.get(magic_le)
    if not kind:
        return {"ok": False, "error": f"unknown magic: {blob[:4].hex()}"}
    out: dict[str, Any] = {"ok": True, "kind": kind, "size": len(blob)}
    if kind.startswith("macho") and len(blob) >= 8:
        cpu = int.from_bytes(blob[4:8], "little" if "swapped" in kind else "big")
        out["cpu_type"] = hex(cpu)
    return out


def _find_urls(strings: list[str], *, limit: int = 32) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in strings:
        for m in _URL_RE.finditer(str(raw or "")):
            url = m.group(0).rstrip(".,;)")
            if url not in seen:
                seen.add(url)
                found.append(url)
            if len(found) >= limit:
                return found
    return found


def _parse_mobileprovision(blob: bytes) -> dict[str, Any]:
    if not blob:
        return {}
    start = blob.find(b"<?xml")
    end = blob.rfind(b"</plist>")
    if start < 0 or end < 0:
        return {"present": True, "parse_error": "plist envelope not found"}
    try:
        data = plistlib.loads(blob[start:end + 8])
    except Exception as exc:
        return {"present": True, "parse_error": str(exc)}
    if not isinstance(data, dict):
        return {"present": True}
    team = data.get("TeamIdentifier") or data.get("TeamName") or ""
    app_id = data.get("ApplicationIdentifierPrefix") or data.get("AppIDName") or ""
    return {
        "present": True,
        "team": team if isinstance(team, str) else list(team)[:4] if isinstance(team, list) else str(team),
        "application_identifier_prefix": app_id if isinstance(app_id, str) else str(app_id),
        "name": str(data.get("Name") or ""),
        "uuid": str(data.get("UUID") or ""),
    }


def _resolve_main_binary(names: list[str], app_prefix: str, executable: str) -> str:
    if app_prefix and executable:
        candidate = f"{app_prefix}{executable}"
        if candidate in names:
            return candidate
    if app_prefix:
        app_name = app_prefix.rstrip("/").rsplit("/", 1)[-1]
        if app_name.endswith(".app"):
            stem = app_name[:-4]
            candidate = f"{app_prefix}{stem}"
            if candidate in names:
                return candidate
        for name in names:
            if name.startswith(app_prefix) and name.count("/") == app_prefix.count("/"):
                rel = name[len(app_prefix):]
                if rel and "/" not in rel and not rel.endswith((".plist", ".png", ".jpg", ".json", ".car", ".nib")):
                    return name
    return ""


def analyze_ipa_path(path: Path, *, max_binary_bytes: int = 8 * 1024 * 1024) -> dict[str, Any]:
    path = path.expanduser().resolve()
    if not path.is_file():
        return {"ok": False, "error": f"file not found: {path}"}

    try:
        digest = sha256_file(path)
    except OSError as exc:
        return {"ok": False, "error": str(exc)}

    result: dict[str, Any] = {
        "ok": True,
        "format": "ipa",
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": digest,
        "confidence": "probable",
        "tools_used": ["zip", "plistlib"],
    }

    try:
        with zipfile.ZipFile(path) as zf:
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            result["zip"] = summarize_zip_entries(zf)
            app_prefix = _find_app_bundle(names)
            result["layout"] = _categorize_ipa_entries(names, app_prefix)
            if app_prefix:
                result["app_bundle"] = app_prefix.rstrip("/")

            plist_name = _find_info_plist(names)
            parsed: dict[str, Any] = {}
            if not plist_name:
                result["info_plist"] = {"error": "Info.plist not found under Payload/"}
                result["confidence"] = "low"
            else:
                blob = zf.read(plist_name)
                parsed = _parse_plist(blob)
                result["info_plist"] = {
                    "entry": plist_name,
                    "size": len(blob),
                    "summary": _extract_plist_summary(parsed) if "_error" not in parsed else {},
                    "parse_error": parsed.get("_error"),
                }
                if parsed.get("_error"):
                    result["confidence"] = "low"
                else:
                    result["confidence"] = "high"

            summary = (result.get("info_plist") or {}).get("summary") or {}
            main_binary = _resolve_main_binary(names, app_prefix, str(summary.get("executable") or ""))
            if main_binary:
                info = zf.getinfo(main_binary)
                read_size = min(info.file_size, max_binary_bytes)
                binary_blob = zf.read(main_binary)[:read_size]
                strings = extract_ascii_strings(binary_blob, limit=400)
                result["main_binary"] = {
                    "entry": main_binary,
                    "size": info.file_size,
                    "truncated_read": info.file_size > read_size,
                    "macho": _macho_summary(binary_blob[:4096]),
                    "strings_preview": strings[:80],
                    "urls": _find_urls(strings),
                }
            else:
                result["main_binary"] = {"error": "main executable not found in app bundle"}

            prov_name = (result.get("layout") or {}).get("embedded_mobileprovision") or ""
            if prov_name:
                try:
                    result["mobileprovision"] = _parse_mobileprovision(zf.read(prov_name))
                except KeyError:
                    result["mobileprovision"] = {"present": False, "error": "entry missing"}
    except zipfile.BadZipFile:
        return {"ok": False, "error": "not a valid zip/ipa archive", "sha256": digest}

    result["findings"] = _triage_findings(result)
    result["risk_tags"] = sorted({f["tag"] for f in result["findings"] if f.get("tag")})
    return result


def extract_main_binary(ipa_path: Path, dest_dir: Path) -> dict[str, Any]:
    """Extract the app main Mach-O binary from an IPA into ``dest_dir``."""
    ipa_path = ipa_path.expanduser().resolve()
    dest_dir = dest_dir.expanduser().resolve()
    if not ipa_path.is_file():
        return {"ok": False, "error": f"file not found: {ipa_path}"}
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(ipa_path) as zf:
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            app_prefix = _find_app_bundle(names)
            plist_name = _find_info_plist(names)
            executable = ""
            if plist_name:
                parsed = _parse_plist(zf.read(plist_name))
                if "_error" not in parsed:
                    executable = str(_extract_plist_summary(parsed).get("executable") or "")
            main_binary = _resolve_main_binary(names, app_prefix, executable)
            if not main_binary:
                return {"ok": False, "error": "main executable not found in IPA"}
            blob = zf.read(main_binary)
            out_name = main_binary.rsplit("/", 1)[-1] or "main"
            out_path = dest_dir / out_name
            out_path.write_bytes(blob)
            return {
                "ok": True,
                "entry": main_binary,
                "path": str(out_path.resolve()),
                "size": len(blob),
                "macho": _macho_summary(blob[:4096]),
            }
    except zipfile.BadZipFile:
        return {"ok": False, "error": "not a valid zip/ipa archive"}


def analyze_ipa_bytes(data: bytes, *, filename: str = "sample.ipa") -> dict[str, Any]:
    import tempfile

    digest = sha256_bytes(data)
    with tempfile.NamedTemporaryFile(suffix=".ipa", delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        out = analyze_ipa_path(tmp_path)
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
    summary = (result.get("info_plist") or {}).get("summary") or {}
    ats = summary.get("ats") or {}
    if isinstance(ats, dict) and ats.get("NSAllowsArbitraryLoads") is True:
        out.append({
            "kind": "note",
            "tag": "ats_disabled",
            "title": "App Transport Security allows arbitrary loads",
            "confidence": "verified",
        })
    if summary.get("url_schemes"):
        out.append({
            "kind": "endpoint",
            "tag": "url_schemes",
            "title": "Custom URL schemes registered",
            "confidence": "verified",
            "details": summary.get("url_schemes")[:10],
        })
    info = result.get("info_plist") or {}
    if info.get("error") or info.get("parse_error"):
        out.append({
            "kind": "note",
            "tag": "plist_unavailable",
            "title": "Info.plist missing or unreadable",
            "confidence": "verified",
            "details": info.get("error") or info.get("parse_error"),
        })

    layout = result.get("layout") or {}
    if layout.get("framework_count", 0) > 0:
        out.append({
            "kind": "note",
            "tag": "embedded_frameworks",
            "title": f"{layout['framework_count']} embedded framework(s)",
            "confidence": "verified",
            "details": layout.get("frameworks", [])[:8],
        })

    main = result.get("main_binary") or {}
    if main.get("entry"):
        macho = main.get("macho") or {}
        out.append({
            "kind": "note",
            "tag": "main_binary",
            "title": f"Main binary: {main['entry'].rsplit('/', 1)[-1]}",
            "confidence": "verified" if macho.get("ok") else "probable",
            "details": macho.get("kind") or main.get("error"),
        })
        urls = main.get("urls") or []
        if urls:
            out.append({
                "kind": "endpoint",
                "tag": "binary_urls",
                "title": "HTTP(S) URLs in main binary strings",
                "confidence": "probable",
                "details": urls[:12],
            })

    prov = result.get("mobileprovision") or {}
    if prov.get("present"):
        out.append({
            "kind": "note",
            "tag": "embedded_provision",
            "title": "Embedded mobile provisioning profile",
            "confidence": "verified",
            "details": prov.get("name") or prov.get("team") or "",
        })

    return out
