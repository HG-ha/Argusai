"""Tests for reverse artifact helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from argusai.core.reverse.reverse_artifacts import (
    ReverseArtifactStore,
    artifact_ref,
    parse_artifact_ref,
    preview_for_llm,
)
from argusai.core.reverse.reverse_jobs import ReverseJobStore


@pytest.fixture
def stores(tmp_path: Path) -> tuple[ReverseJobStore, ReverseArtifactStore]:
    root = ReverseJobStore(tmp_path / "reverse_jobs")
    return root, ReverseArtifactStore(root)


def test_artifact_ref_roundtrip() -> None:
    ref = artifact_ref("rev-20260523-abcd", "art-xyz123")
    parsed = parse_artifact_ref(ref)
    assert parsed == ("rev-20260523-abcd", "art-xyz123")
    assert parse_artifact_ref("invalid") is None


def test_register_path_and_preview(stores: tuple[ReverseJobStore, ReverseArtifactStore], tmp_path: Path) -> None:
    store, artifacts = stores
    job = store.create(title="artifact test", target_type="android", platform="android")
    src = tmp_path / "sample.apk"
    src.write_bytes(b"PK\x03\x04fake-apk")

    record = artifacts.register_path(job["id"], source_path=src, kind="apk", tool="import")
    assert record["artifact_ref"].startswith("rev://")
    assert record["kind"] == "apk"

    preview = preview_for_llm(store, job["id"], record["id"], max_bytes=64)
    assert preview["ok"] is True
    assert preview["truncated"] is False
    assert "read_more" in preview
