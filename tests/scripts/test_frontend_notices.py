from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "frontend_notices", Path(__file__).resolve().parents[2] / "scripts/frontend_notices.py"
)
notices = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notices)


@pytest.fixture
def webui(tmp_path):
    root = tmp_path / "webui"
    root.mkdir()
    (root / "package-lock.json").write_text(json.dumps({
        "lockfileVersion": 3,
        "packages": {"": {"name": "fixture", "dependencies": {"sample": "1.0.0"}}},
    }), encoding="utf-8")
    return root


def add_package(webui, key="node_modules/sample", name="sample", version="1.0.0", *, license_text="Original permission\nOriginal disclaimer\n", **entry):
    lock_path = webui / "package-lock.json"
    lock = json.loads(lock_path.read_text())
    lock["packages"][key] = {"version": version, "resolved": f"https://example.test/{name}-{version}.tgz", **entry}
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    package = webui / key
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": name, "version": version, "license": "MIT"}), encoding="utf-8")
    if license_text is not None:
        (package / "LICENSE").write_bytes(license_text.encode("utf-8"))
    return package


def test_original_license_notice_and_nested_dependency_are_preserved(webui):
    original = "Copyright (c) An actual upstream\r\nPermission in full.\r\nDISCLAIMER IN FULL.\r\n"
    package = add_package(webui, license_text=original)
    (package / "NOTICE").write_text("Additional upstream attribution\n", encoding="utf-8")
    add_package(webui, "node_modules/sample/node_modules/@scope/nested", "@scope/nested")
    # Dev-only packages need not be installed.
    lock = json.loads((webui / "package-lock.json").read_text())
    lock["packages"]["node_modules/dev-only"] = {"version": "9.0.0", "dev": True}
    (webui / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    output = notices.generate_notices(webui)
    data = output.read_bytes()
    assert original.encode("utf-8") in data
    assert b"Additional upstream attribution" in data
    assert b"@scope/nested@1.0.0" in data
    assert b"dev-only@" not in data
    assert hashlib.sha256((webui / "package-lock.json").read_bytes()).hexdigest().encode() in data
    notices.check_notices(webui)


def test_unknown_missing_license_blocks_generation_even_with_spdx_metadata(webui):
    package = add_package(webui, license_text=None)
    (package / "NOTICE").write_text("Only a notice; no license grant", encoding="utf-8")
    with pytest.raises(ValueError, match="Missing original license text"):
        notices.generate_notices(webui)
    assert not (webui / "dist").exists()


def test_missing_package_and_installed_version_drift_fail(webui):
    package = add_package(webui)
    (package / "package.json").unlink()
    with pytest.raises(OSError):
        notices.generate_notices(webui)
    (package / "package.json").write_text(json.dumps({"name": "sample", "version": "2.0.0"}))
    with pytest.raises(ValueError, match="name/version does not match lock"):
        notices.generate_notices(webui)


def test_check_rejects_lock_changes_and_notice_text_changes(webui):
    package = add_package(webui)
    notices.generate_notices(webui)
    lock_path = webui / "package-lock.json"
    lock_path.write_bytes(lock_path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="stale or modified"):
        notices.check_notices(webui)
    notices.generate_notices(webui)
    (package / "LICENSE").write_text("Updated upstream copyright and grant")
    with pytest.raises(ValueError, match="stale or modified"):
        notices.check_notices(webui)


def test_check_is_read_only_and_rejects_missing_output(webui):
    add_package(webui)
    with pytest.raises(ValueError, match="Missing frontend notices"):
        notices.check_notices(webui)
    assert not (webui / "dist").exists()


@pytest.mark.parametrize("key", [
    "../outside", "node_modules/../../outside", "node_modules/x/../sample",
    "node_modules\\sample", "/node_modules/sample", "C:/node_modules/sample",
    "node_modules/@scope/../outside", "node_modules//sample",
])
def test_lock_paths_cannot_escape_node_modules(webui, key):
    lock = json.loads((webui / "package-lock.json").read_text())
    lock["packages"][key] = {"version": "1.0.0", "dev": True}
    (webui / "package-lock.json").write_text(json.dumps(lock))
    (webui / "node_modules").mkdir()
    with pytest.raises(ValueError, match="Invalid package lock path"):
        notices.generate_notices(webui)


def test_license_symlink_cannot_escape_package(webui, tmp_path):
    package = add_package(webui, license_text=None)
    outside = tmp_path / "external-license.txt"
    outside.write_text("Not the installed package license")
    try:
        (package / "LICENSE").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable on this platform")
    with pytest.raises(ValueError, match="Path escapes"):
        notices.generate_notices(webui)


def test_fixed_upstream_fallback_preserves_actual_copyright_and_rejects_new_version(webui):
    package = add_package(webui, name="css-render", key="node_modules/css-render", version="0.15.14", license_text=None)
    output = notices.generate_notices(webui).read_text(encoding="utf-8")
    original = (notices.ROOT / "scripts/packaging/css-render-0.15.14.LICENSE.txt").read_text(encoding="utf-8")
    assert original in output
    assert "Copyright (c) 2020 07akioni" in output
    assert "4fd3b95ecedb17bffafabc00e93ce6eab843e716/LICENSE" in output
    metadata = json.loads((package / "package.json").read_text())
    metadata["version"] = "0.15.15"
    (package / "package.json").write_text(json.dumps(metadata))
    lock = json.loads((webui / "package-lock.json").read_text())
    lock["packages"]["node_modules/css-render"]["version"] = "0.15.15"
    (webui / "package-lock.json").write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="no version-matched fallback"):
        notices.generate_notices(webui)


def test_metadata_only_exception_warns_and_never_invents_copyright(webui):
    package = add_package(webui, name="vdirs", key="node_modules/vdirs", version="0.1.8", license_text=None)
    metadata = json.loads((package / "package.json").read_text())
    metadata["author"] = "07akioni"
    (package / "package.json").write_text(json.dumps(metadata))
    with pytest.warns(notices.NoticeReviewWarning, match="UPSTREAM LICENSE TEXT MISSING"):
        output = notices.generate_notices(webui).read_text(encoding="utf-8")
    assert "REFERENCE ONLY; upstream original missing" in output
    assert "Copyright (c) <year> <copyright holders>" in output
    assert "Copyright (c) 2020 07akioni" not in output
    assert '"author": "07akioni"' in output
    with pytest.warns(notices.NoticeReviewWarning):
        notices.check_notices(webui)
    metadata["author"] = "Unexpected change"
    (package / "package.json").write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="Metadata-only exception no longer matches"):
        notices.generate_notices(webui)


def test_fallback_checksum_corruption_fails(webui, tmp_path, monkeypatch):
    add_package(webui, name="css-render", key="node_modules/css-render", version="0.15.14", license_text=None)
    record = json.loads(notices.FALLBACK_MANIFEST.read_text())["packages"]["css-render"]
    manifest = tmp_path / "fallbacks.json"
    manifest.write_text(json.dumps({"packages": {"css-render": record}}))
    (tmp_path / record["file"]).write_text("Incomplete or tampered license")
    monkeypatch.setattr(notices, "FALLBACK_MANIFEST", manifest)
    with pytest.raises(ValueError, match="checksum mismatch"):
        notices.generate_notices(webui)
