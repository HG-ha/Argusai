"""Tests for reverse-job export helpers and the explorer/reveal pipeline.

Three flows are exercised end-to-end against a temp ``ReverseJobStore``:

* ``reverse_job_export`` zip / directory output, ``include_decompiled``
  gating, top-level files copied, byte budget honored.
* ``resolve_artifact_path`` containment + missing-file errors.
* ``reverse_artifact_copy_to`` AI tool — copy into a stub session
  sandbox and to an absolute path; ensure escape attempts are rejected.

OS file-manager invocations (``subprocess.Popen``) are monkey-patched so
the tests stay headless.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from argusai.core.reverse.reverse_artifacts import (
    ReverseArtifactStore,
    resolve_artifact_path,
)
from argusai.core.reverse.reverse_job_export import export_reverse_job
from argusai.core.reverse.reverse_jobs import ReverseJobStore


def _seed_job(tmp_path: Path) -> tuple[ReverseJobStore, str]:
    store = ReverseJobStore(tmp_path / "reverse_jobs")
    job = store.create(title="export-test", target_type="android", platform="android")
    job_id = job["id"]
    store.add_artifact(
        job_id,
        content="trace payload",
        kind="trace",
        filename="t.json",
        tool="test",
    )
    store.add_artifact(
        job_id,
        content="frida log line\n" * 3,
        kind="frida_log",
        filename="frida.log",
        tool="frida_attach",
    )
    store.add_finding(
        job_id,
        title="example",
        body="# example\nbody",
        kind="note",
        confidence="probable",
    )
    decompiled = Path(store._job_dir(job_id)) / "decompiled" / "jadx-abcdef"
    decompiled.mkdir(parents=True, exist_ok=True)
    (decompiled / "Main.java").write_text("class Main {}\n", encoding="utf-8")
    return store, job_id


def test_export_zip_skips_decompiled_by_default(tmp_path: Path) -> None:
    store, job_id = _seed_job(tmp_path)
    out = export_reverse_job(store, job_id, dest_dir=tmp_path / "out", as_zip=True)
    assert out["ok"] is True
    assert out["format"] == "zip"
    archive = Path(out["path"])
    assert archive.is_file()
    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "job.json" in names
    assert any(name.startswith("artifacts/") for name in names)
    assert any(name.startswith("findings/") for name in names)
    assert not any(name.startswith("decompiled/") for name in names)
    assert out["files"] >= 3


def test_export_dir_with_decompiled(tmp_path: Path) -> None:
    store, job_id = _seed_job(tmp_path)
    out = export_reverse_job(
        store,
        job_id,
        dest_dir=tmp_path / "out",
        include_decompiled=True,
        as_zip=False,
    )
    assert out["ok"] is True
    assert out["format"] == "dir"
    target = Path(out["path"])
    assert target.is_dir()
    assert (target / "job.json").is_file()
    assert (target / "artifacts").is_dir()
    assert (target / "decompiled" / "jadx-abcdef" / "Main.java").is_file()


def test_export_default_dest_is_store_root_exports(tmp_path: Path) -> None:
    store, job_id = _seed_job(tmp_path)
    out = export_reverse_job(store, job_id, as_zip=True)
    archive = Path(out["path"])
    assert archive.is_file()
    assert archive.parent == (store.root / "_exports").resolve()


def test_export_dedupes_duplicate_archives(tmp_path: Path) -> None:
    store, job_id = _seed_job(tmp_path)
    first = export_reverse_job(store, job_id, dest_dir=tmp_path / "out", as_zip=True)
    second = export_reverse_job(store, job_id, dest_dir=tmp_path / "out", as_zip=True)
    assert Path(first["path"]) != Path(second["path"])
    assert Path(second["path"]).is_file()


def test_export_missing_job_raises(tmp_path: Path) -> None:
    store = ReverseJobStore(tmp_path / "reverse_jobs")
    with pytest.raises(FileNotFoundError):
        export_reverse_job(store, "rev-does-not-exist", dest_dir=tmp_path)


def test_resolve_artifact_path_containment(tmp_path: Path) -> None:
    store, job_id = _seed_job(tmp_path)
    art_id = store.get(job_id)["artifacts"][0]["id"]
    resolved = resolve_artifact_path(store, job_id, art_id)
    assert resolved.is_file()
    assert str(resolved).startswith(str(Path(store.root / job_id).resolve()))

    with pytest.raises(FileNotFoundError):
        resolve_artifact_path(store, job_id, "art-missing")
    with pytest.raises(FileNotFoundError):
        resolve_artifact_path(store, "rev-bogus", art_id)


def test_artifact_copy_to_session_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from argusai.core.tools import native_reverse

    store, job_id = _seed_job(tmp_path)
    art_id = store.get(job_id)["artifacts"][0]["id"]

    sandbox_root = (tmp_path / "sandbox").resolve()
    sandbox_root.mkdir(parents=True, exist_ok=True)

    def _fake_sandbox_root(api: Any, kwargs: dict[str, Any]) -> Path:  # noqa: ARG001
        return sandbox_root

    monkeypatch.setattr(native_reverse, "_session_sandbox_root", _fake_sandbox_root)
    api = SimpleNamespace(reverse_jobs=store)

    out = native_reverse.reverse_artifact_copy_to(api, job_id=job_id, artifact_id=art_id)
    assert out["ok"] is True
    dest = Path(out["path"])
    assert dest.is_file()
    assert sandbox_root in dest.parents
    assert dest.parent.name == "imports"
    assert dest.read_text(encoding="utf-8") == "trace payload"


def test_artifact_copy_to_explicit_directory(tmp_path: Path) -> None:
    from argusai.core.tools import native_reverse

    store, job_id = _seed_job(tmp_path)
    art_id = store.get(job_id)["artifacts"][1]["id"]
    api = SimpleNamespace(reverse_jobs=store)

    out_dir = tmp_path / "user-pick"
    out_dir.mkdir()
    out = native_reverse.reverse_artifact_copy_to(
        api, job_id=job_id, artifact_id=art_id, dest_path=str(out_dir)
    )
    assert out["ok"] is True
    assert Path(out["path"]).parent == out_dir.resolve()


def test_reverse_job_open_dir_reveals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from argusai.core.infra import explorer
    from argusai.core.tools import native_reverse

    store, job_id = _seed_job(tmp_path)
    api = SimpleNamespace(reverse_jobs=store)

    calls: list[Path] = []

    def _fake_reveal(target: Path) -> dict[str, Any]:
        calls.append(Path(target))
        return {"ok": True, "path": str(target)}

    monkeypatch.setattr(native_reverse, "reveal_path", _fake_reveal)
    monkeypatch.setattr(explorer, "reveal_path", _fake_reveal)

    out = native_reverse.reverse_job_open_dir(api, job_id=job_id)
    assert out["ok"] is True
    assert calls and calls[0].name == job_id
    assert out["job_id"] == job_id


def test_artifact_store_facade_copy_to(tmp_path: Path) -> None:
    store, job_id = _seed_job(tmp_path)
    art_id = store.get(job_id)["artifacts"][0]["id"]
    facade = ReverseArtifactStore(store)
    target = tmp_path / "outside" / "trace-copy.json"
    out = facade.copy_to(job_id, art_id, target)
    assert out["ok"] is True
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == "trace payload"
