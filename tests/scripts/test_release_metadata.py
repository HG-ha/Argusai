from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import subprocess
import tarfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SOURCE_FILES = {
    "LICENSE": b"GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n",
    "THIRD_PARTY_NOTICES.md": b"Third-party notices fixture\n",
    "build.py": b"# committed build script\n",
    "pyproject.toml": b"[project]\nname = 'fixture'\n",
    "uv.lock": b"version = 1\n",
    "argusai/webui/package.json": b'{"version":"0.1.0"}\n',
    "argusai/webui/package-lock.json": b'{"lockfileVersion":3}\n',
    "argusai/webui/src/main.js": b"// frontend entry\n",
    "argusai/webui/src/components/App.vue": b"<template>fixture</template>\n",
    "argusai/webui/src/api.js": b"// API bridge\n",
    "scripts/frontend_notices.py": b"# notices fixture\n",
    "scripts/python_notices.py": b"# Python notices fixture\n",
    "scripts/packaging/frontend-licenses.json": b"{}\n",
    "scripts/packaging/python-licenses.json": b"{}\n",
    "argusai/vendor/sqlmap/LICENSE": b"GPL vendor license fixture\n",
    "argusai/vendor/sqlmap/sqlmap.py": b"# vendor fixture\n",
    "scripts/bump_version.py": b"# version fixture\n",
    ".github/workflows/release-packages.yml": b"name: fixture\n",
}
spec = importlib.util.spec_from_file_location("release_version", ROOT / "scripts/bump_version.py")
versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versions)


@pytest.fixture
def metadata(monkeypatch, tmp_path):
    init = tmp_path / "__init__.py"
    package = tmp_path / "package.json"
    lock = tmp_path / "package-lock.json"
    init.write_bytes(b'__version__ = "0.1.0"\n')
    package.write_text(json.dumps({"version": "0.1.0"}), encoding="utf-8")
    lock.write_text(json.dumps({"version": "0.1.0", "packages": {"": {"version": "0.1.0"}}}), encoding="utf-8")
    monkeypatch.setattr(versions, "INIT", init)
    monkeypatch.setattr(versions, "PACKAGE_JSON", package)
    monkeypatch.setattr(versions, "PACKAGE_LOCK", lock)
    return init, package, lock


@pytest.mark.parametrize("tag", ["v0.1.0", "0.1.0", None])
def test_release_check_is_read_only(metadata, tag):
    before = [path.read_bytes() for path in metadata]
    assert versions.check_version(tag) == "0.1.0"
    assert before == [path.read_bytes() for path in metadata]


def test_release_rejects_mismatched_tag(metadata):
    with pytest.raises(SystemExit, match="does not match"):
        versions.check_version("v9.9.9")


@pytest.mark.parametrize("location", ["package", "lock", "root-package"])
def test_release_rejects_inconsistent_metadata(metadata, location):
    _, package, lock = metadata
    path = package if location == "package" else lock
    data = json.loads(path.read_text())
    if location == "root-package":
        data["packages"][""]["version"] = "9.9.9"
    else:
        data["version"] = "9.9.9"
    path.write_text(json.dumps(data))
    with pytest.raises(SystemExit, match="Version mismatch"):
        versions.check_version()


def test_bump_updates_all_versions_and_preserves_line_endings(metadata):
    init, _, _ = metadata
    init.write_bytes(b'__version__ = "0.1.0"\r\n')
    versions.set_version("0.1.1")
    assert versions.check_version("v0.1.1") == "0.1.1"
    assert init.read_bytes() == b'__version__ = "0.1.1"\r\n'


def workflow():
    # BaseLoader keeps the GitHub `on` key as text rather than YAML 1.1 boolean.
    return yaml.load((ROOT / ".github/workflows/release-packages.yml").read_text(), Loader=yaml.BaseLoader)


def test_required_release_sources_exist_in_actual_project():
    for name in SOURCE_FILES:
        assert (ROOT / name).is_file(), name
        assert (ROOT / name).stat().st_size, name


def test_project_license_metadata_and_full_original_text():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "GPL-3.0-only"
    assert {"LICENSE", "THIRD_PARTY_NOTICES.md"} <= set(project["license-files"])
    frontend = json.loads((ROOT / "argusai/webui/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "argusai/webui/package-lock.json").read_text(encoding="utf-8"))
    assert frontend["license"] == lock["packages"][""]["license"] == project["license"]
    # Exact unmodified text downloaded from https://www.gnu.org/licenses/gpl-3.0.txt.
    assert hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() == "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"


def python_step(job, name):
    step = next(step for step in workflow()["jobs"][job]["steps"] if step.get("name") == name)
    return compile(step["run"], f"workflow:{name}", "exec")


def test_release_waits_for_all_platforms_and_manual_runs_never_publish():
    config = workflow()
    assert config["on"]["release"]["types"] == ["published"]
    assert config["permissions"] == {"contents": "read"}
    publish = config["jobs"]["publish"]
    assert publish["needs"] == ["validate", "build", "source"]
    assert publish["if"] == "github.event_name == 'release'"
    assert publish["permissions"] == {"contents": "write"}
    build = config["jobs"]["build"]
    assert build["strategy"]["fail-fast"] == "false"
    assert {row["runtime"] for row in build["strategy"]["matrix"]["include"]} == {"win-x64", "macos-arm64", "linux-x64"}
    source = config["jobs"]["source"]
    assert source["needs"] == "validate"
    assert source["steps"][0]["with"]["ref"] == "${{ needs.validate.outputs.commit }}"
    assert "if" not in source  # Manual runs get a downloadable source artifact too.
    upload = next(step for step in source["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == "ArgusAI-${{ needs.validate.outputs.version }}-source"
    assert upload["with"]["if-no-files-found"] == "error"
    for job in config["jobs"].values():
        for step in job["steps"]:
            if step.get("shell") == "python":
                compile(step["run"], "workflow-inline-python", "exec")


def write_checksum(package):
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    Path(str(package) + ".sha256").write_text(f"{digest}  {package.name}\n", encoding="utf-8")


def write_source_archive(directory, *, commit="a" * 40, omit=None, extra=None):
    package = directory / "ArgusAI-0.1.0-source.tar.gz"
    contents = {**SOURCE_FILES, **(extra or {})}
    with tarfile.open(package, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}) as archive:
        for name, data in contents.items():
            if name == omit:
                continue
            member = tarfile.TarInfo(f"ArgusAI-0.1.0/{name}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    write_checksum(package)
    return package


def publish_assets(directory):
    for suffix in ("win-x64-setup.exe", "macos-arm64.dmg", "linux-x64.tar.gz"):
        package = directory / f"ArgusAI-0.1.0-{suffix}"
        package.write_bytes(b"native-package-fixture")
        write_checksum(package)
    return write_source_archive(directory)


@pytest.mark.parametrize("corrupt", [False, True])
def test_release_verifies_actual_package_bytes(monkeypatch, tmp_path, corrupt):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path.parent / f"{tmp_path.name}-summary.md"))
    monkeypatch.setenv("RELEASE_VERSION", "0.1.0")
    monkeypatch.setenv("SOURCE_COMMIT", "a" * 40)
    packages = []
    for suffix in ("win-x64-setup.exe", "macos-arm64.dmg", "linux-x64.tar.gz"):
        package = tmp_path / f"ArgusAI-0.1.0-{suffix}"
        package.write_bytes(b"native-package-fixture")
        monkeypatch.setenv("PACKAGE_PATH", str(package))
        exec(python_step("build", "Verify package and create SHA-256 checksum"), {})
        packages.append(package)
    write_source_archive(tmp_path)
    if corrupt:
        packages[-1].write_bytes(b"changed-after-checksum")
        with pytest.raises(SystemExit, match="Checksum mismatch"):
            exec(python_step("publish", "Verify source, all three packages and checksums"), {})
    else:
        exec(python_step("publish", "Verify source, all three packages and checksums"), {})


@pytest.fixture
def source_repository(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    hooks = repository / ".git/fixture-empty-hooks"
    hooks.mkdir()
    subprocess.run(["git", "-C", str(repository), "config", "core.hooksPath", str(hooks)], check=True)
    for name, content in SOURCE_FILES.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Release fixture",
                    "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                    "commit", "-m", "fixture source"], check=True, capture_output=True)
    commit = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.chdir(repository)
    monkeypatch.setenv("RELEASE_VERSION", "0.1.0")
    monkeypatch.setenv("SOURCE_COMMIT", commit)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "source-summary.md"))
    return repository, commit


def test_source_archive_uses_validated_commit_and_excludes_untracked_secrets(source_repository):
    repository, commit = source_repository
    # Neither local edits nor untracked runtime credentials belong to a release commit.
    (repository / "build.py").write_text("# uncommitted change\n")
    (repository / "settings.json").write_text('{"secret":"not-for-release"}')
    (repository / "config").mkdir()
    (repository / "config/api-key.txt").write_text("not-for-release")
    exec(python_step("source", "Archive matching source and create SHA-256 checksum"), {})
    package = repository / "dist/ArgusAI-0.1.0-source.tar.gz"
    with tarfile.open(package, "r:gz") as archive:
        assert archive.pax_headers["comment"] == commit
        names = set(archive.getnames())
        for name, original in SOURCE_FILES.items():
            assert archive.extractfile(f"ArgusAI-0.1.0/{name}").read() == original
        assert not any(".git" in Path(name).parts for name in names)
        assert "ArgusAI-0.1.0/settings.json" not in names
        assert "ArgusAI-0.1.0/config/api-key.txt" not in names
    assert Path(str(package) + ".sha256").read_text().strip() == f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}"


def test_source_job_rejects_a_different_checkout(source_repository, monkeypatch):
    repository, _ = source_repository
    monkeypatch.setenv("SOURCE_COMMIT", "b" * 40)
    with pytest.raises(SystemExit, match="checkout does not match validated commit"):
        exec(python_step("source", "Archive matching source and create SHA-256 checksum"), {})
    assert not (repository / "dist").exists()


def test_source_job_rejects_missing_tracked_notices(source_repository, monkeypatch):
    repository, _ = source_repository
    subprocess.run(["git", "rm", "THIRD_PARTY_NOTICES.md"], check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Release fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-m", "fixture missing notice"],
                   check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setenv("SOURCE_COMMIT", commit)
    with pytest.raises(SystemExit, match="Missing or empty tracked source file: THIRD_PARTY_NOTICES.md"):
        exec(python_step("source", "Archive matching source and create SHA-256 checksum"), {})
    assert not (repository / "dist/ArgusAI-0.1.0-source.tar.gz.sha256").exists()


def test_source_job_blocks_accidentally_tracked_runtime_settings(source_repository, monkeypatch):
    repository, _ = source_repository
    (repository / "settings.json").write_text('{"secret":"private"}')
    subprocess.run(["git", "add", "settings.json"], check=True)
    subprocess.run(["git", "-c", "user.name=Release fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-m", "fixture accidental private data"],
                   check=True, capture_output=True)
    monkeypatch.setenv("SOURCE_COMMIT", subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    with pytest.raises(SystemExit, match="Private runtime path"):
        exec(python_step("source", "Archive matching source and create SHA-256 checksum"), {})
    assert not (repository / "dist/ArgusAI-0.1.0-source.tar.gz.sha256").exists()


@pytest.mark.parametrize("failure, message", [
    ("missing", "Missing or empty release package"),
    ("tampered", "Checksum mismatch"),
    ("missing-checksum", "Missing release checksum"),
    ("different-commit", "does not identify the validated commit"),
    ("missing-license", "Missing or empty tracked source file: LICENSE"),
    ("private-runtime", "Private runtime path"),
])
def test_source_failures_stop_publish(monkeypatch, tmp_path, failure, message):
    source = publish_assets(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASE_VERSION", "0.1.0")
    monkeypatch.setenv("SOURCE_COMMIT", "a" * 40)
    if failure == "missing":
        source.unlink()
    elif failure == "tampered":
        source.write_bytes(source.read_bytes() + b"changed-after-checksum")
    elif failure == "missing-checksum":
        Path(str(source) + ".sha256").unlink()
    elif failure == "different-commit":
        write_source_archive(tmp_path, commit="b" * 40)
    elif failure == "missing-license":
        write_source_archive(tmp_path, omit="LICENSE")
    elif failure == "private-runtime":
        write_source_archive(tmp_path, extra={"settings.json": b"private"})
    with pytest.raises(SystemExit, match=message):
        exec(python_step("publish", "Verify source, all three packages and checksums"), {})


def test_missing_package_stops_before_upload(monkeypatch, tmp_path):
    monkeypatch.setenv("PACKAGE_PATH", str(tmp_path / "missing.exe"))
    with pytest.raises(SystemExit, match="Missing or empty"):
        exec(python_step("build", "Verify package and create SHA-256 checksum"), {})


@pytest.mark.parametrize("binary_version", ["0.1.0", "9.9.9"])
def test_packaged_cli_must_report_the_expected_version(monkeypatch, binary_version):
    import platform
    import subprocess

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("RELEASE_VERSION", "0.1.0")
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps({"ok": True, "version": binary_version}))
    monkeypatch.setattr(subprocess, "run", fake_run)
    code = python_step("build", "Smoke-test packaged CLI and version")
    if binary_version == "0.1.0":
        exec(code, {})
    else:
        with pytest.raises(SystemExit, match="does not match"):
            exec(code, {})
    assert calls[0][-2:] == ["version", "--json"]
