"""Integration smoke test for ReverseJob lifecycle (no network / external CLIs).

Run from project root::

    uv run python scripts/smoke_reverse_jobs.py
"""

from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from argusai.core.reverse.android_static import analyze_apk_path
from argusai.core.reverse.ios_static import analyze_ipa_path
from argusai.core.reverse.elf_static import analyze_elf_bytes
from argusai.core.reverse.windows_static import analyze_pe_bytes
from argusai.core.reverse.reverse_job_export import export_reverse_job
from argusai.core.reverse.reverse_jobs import ReverseJobStore
from argusai.core.tools import native_reverse


def _mini_apk(path: Path) -> None:
    manifest = (
        b"\x00\x03" + b"com.example.demo\x00" * 2
        + b"android.permission.INTERNET\x00"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("AndroidManifest.xml", manifest)
        zf.writestr("classes.dex", b"dex\n035\x00")


def _mini_ipa(path: Path) -> None:
    plist = b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.example.demo</string>
<key>CFBundleExecutable</key><string>Demo</string>
</dict></plist>"""
    binary = b"\xcf\xfa\xed\xfe" + b"\x00" * 32 + b"https://api.example.com/v1\x00"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Demo.app/Info.plist", plist)
        zf.writestr("Payload/Demo.app/Demo", binary)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="argusai-reverse-smoke-") as tmp:
        root = Path(tmp)
        store = ReverseJobStore(root / "reverse_jobs")
        job = store.create(title="smoke", target_type="mixed", platform="unknown")
        job_id = job["id"]

        apk = root / "sample.apk"
        ipa = root / "sample.ipa"
        _mini_apk(apk)
        _mini_ipa(ipa)

        apk_analysis = analyze_apk_path(apk)
        ipa_analysis = analyze_ipa_path(ipa)
        pe_analysis = analyze_pe_bytes(b"not a pe")
        elf_analysis = analyze_elf_bytes(b"not elf")
        assert apk_analysis["ok"] is True
        assert ipa_analysis["ok"] is True
        assert pe_analysis["ok"] is False
        assert elf_analysis["ok"] is False

        store.add_artifact(job_id, content=apk.read_bytes(), kind="apk", filename="sample.apk", tool="smoke")
        store.add_artifact(job_id, content=ipa.read_bytes(), kind="ipa", filename="sample.ipa", tool="smoke")
        store.add_finding(job_id, title="smoke-note", body="ok", kind="note", confidence="verified")

        decompiled = store._job_dir(job_id) / "decompiled" / "smoke-tree"
        decompiled.mkdir(parents=True, exist_ok=True)
        (decompiled / "secret.txt").write_text("FLAG{smoke}", encoding="utf-8")

        api = type("Api", (), {"reverse_jobs": store})()
        grep_out = native_reverse.reverse_artifact_grep(api, job_id=job_id, pattern=r"FLAG\{smoke\}")
        assert grep_out["ok"] is True
        assert grep_out.get("match_count_returned", 0) >= 1

        export_out = export_reverse_job(store, job_id, dest_dir=root / "export", as_zip=True)
        assert export_out["ok"] is True
        assert Path(export_out["path"]).is_file()

        print(f"reverse smoke OK · job_id={job_id} · grep_hits={grep_out.get('match_count_returned')} · export={export_out['path']}")


if __name__ == "__main__":
    main()
