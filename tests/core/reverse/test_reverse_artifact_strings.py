"""Tests for reverse_artifact_extract_strings."""

from __future__ import annotations

import json
from pathlib import Path

from argusai.core.reverse.reverse_artifacts import ReverseArtifactStore
from argusai.core.reverse.reverse_jobs import ReverseJobStore
from argusai.core.tools import EXECUTORS
from argusai.core.tools.native_reverse import reverse_artifact_extract_strings


def test_executor_registered() -> None:
    assert "reverse_artifact_extract_strings" in EXECUTORS


def test_extract_strings_from_single_artifact(tmp_path: Path) -> None:
    store = ReverseJobStore(tmp_path / "reverse_jobs")
    artifacts = ReverseArtifactStore(store)
    job = store.create(title="strings", target_type="android", platform="android")
    src = tmp_path / "sample.smali"
    src.write_text(
        'const-string v0, "https://api.example.com/v1/login"\n',
        encoding="utf-8",
    )
    record = artifacts.register_path(job["id"], source_path=src, kind="other", tool="test")
    api = type("Api", (), {"reverse_jobs": store})()

    out = reverse_artifact_extract_strings(
        api,
        job_id=job["id"],
        artifact_id=record["id"],
        kind="url",
        max_results=10,
    )

    assert out["ok"] is True
    assert any("api.example.com" in str(item.get("value") or "") for item in out.get("items") or [])


def test_extract_strings_from_decompile_index(tmp_path: Path) -> None:
    store = ReverseJobStore(tmp_path / "reverse_jobs")
    artifacts = ReverseArtifactStore(store)
    job = store.create(title="decompile", target_type="android", platform="android")
    job_dir = (store.root / job["id"]).resolve()
    out_dir = job_dir / "decompiled" / "apktool-abc"
    smali_dir = out_dir / "smali"
    smali_dir.mkdir(parents=True)
    (smali_dir / "Main.smali").write_text(
        'const-string v1, "https://secret.example/internal"\n',
        encoding="utf-8",
    )
    index = {
        "ok": True,
        "tool": "android_apktool_decode",
        "output_dir": str(out_dir.resolve()),
        "tree": {"file_count": 1},
    }
    index_src = tmp_path / "decompile-android_apktool_decode.json"
    index_src.write_text(json.dumps(index), encoding="utf-8")
    record = artifacts.register_path(
        job["id"],
        source_path=index_src,
        kind="trace",
        tool="android_apktool_decode",
        note="decompile index",
    )
    api = type("Api", (), {"reverse_jobs": store})()

    out = reverse_artifact_extract_strings(
        api,
        job_id=job["id"],
        artifact_id=record["id"],
        kind="url",
        max_results=20,
    )

    assert out["ok"] is True
    assert out.get("decompile_output_dir") == str(out_dir.resolve())
    assert out.get("files_scanned", 0) >= 1
    assert any("secret.example" in str(item.get("value") or "") for item in out.get("items") or [])
