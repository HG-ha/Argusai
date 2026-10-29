"""Tests for reverse_artifact_grep."""

from __future__ import annotations

import json
from pathlib import Path

from argusai.core.reverse.reverse_artifacts import ReverseArtifactStore
from argusai.core.reverse.reverse_jobs import ReverseJobStore
from argusai.core.tools import EXECUTORS
from argusai.core.tools.native_reverse import reverse_artifact_grep


def test_executor_registered() -> None:
    assert "reverse_artifact_grep" in EXECUTORS


def test_grep_decompiled_tree(tmp_path: Path) -> None:
    store = ReverseJobStore(tmp_path / "reverse_jobs")
    artifacts = ReverseArtifactStore(store)
    job = store.create(title="grep", target_type="android", platform="android")
    job_dir = (store.root / job["id"]).resolve()
    out_dir = job_dir / "decompiled" / "apktool-abc"
    strings = out_dir / "res" / "values"
    strings.mkdir(parents=True)
    (strings / "strings.xml").write_text(
        '<resources><string name="app_name">SecretAppName</string></resources>',
        encoding="utf-8",
    )
    api = type("Api", (), {"reverse_jobs": store})()

    out = reverse_artifact_grep(api, job_id=job["id"], pattern="SecretAppName")

    assert out["ok"] is True
    assert out.get("scan_root") == str((job_dir / "decompiled").resolve())
    matches = out.get("matches") or []
    blob = json.dumps(matches, ensure_ascii=False)
    assert "SecretAppName" in blob or "strings.xml" in blob


def test_grep_via_decompile_index(tmp_path: Path) -> None:
    store = ReverseJobStore(tmp_path / "reverse_jobs")
    artifacts = ReverseArtifactStore(store)
    job = store.create(title="grep-index", target_type="android", platform="android")
    job_dir = (store.root / job["id"]).resolve()
    out_dir = job_dir / "decompiled" / "jadx-xyz"
    out_dir.mkdir(parents=True)
    (out_dir / "Main.java").write_text('String url = "https://api.example.com/v1";', encoding="utf-8")
    index = {
        "ok": True,
        "tool": "android_jadx_decompile",
        "output_dir": str(out_dir.resolve()),
    }
    index_src = tmp_path / "decompile-index.json"
    index_src.write_text(json.dumps(index), encoding="utf-8")
    record = artifacts.register_path(
        job["id"],
        source_path=index_src,
        kind="trace",
        tool="android_jadx_decompile",
    )
    api = type("Api", (), {"reverse_jobs": store})()

    out = reverse_artifact_grep(
        api,
        job_id=job["id"],
        artifact_id=record["id"],
        pattern="api\\.example\\.com",
    )

    assert out["ok"] is True
    assert out.get("decompile_output_dir") == str(out_dir.resolve())
