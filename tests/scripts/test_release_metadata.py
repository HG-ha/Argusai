from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
NATIVE_SUFFIXES = ("win-x64-setup.exe", "win-arm64-setup.exe", "macos-arm64.dmg", "linux-x64.tar.gz", "linux-arm64.tar.gz", "shell-win-x64.zip", "shell-win-arm64.zip", "shell-macos-arm64.tar.gz", "shell-linux-x64.tar.gz", "shell-linux-arm64.tar.gz")
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
    "scripts/packaging/python-pyobjc-12.1.LICENSE.txt": b"PyObjC license fixture\n",
    "scripts/packaging/release-notes-0.1.0.md": b"# ArgusAI v0.1.0\n\nRelease notes fixture.\n",
    "argusai/gui/windows-arm64.runtimeconfig.json": b'{"runtimeOptions":{"tfm":"net8.0-windows"}}\n',
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
    return yaml.load((ROOT / ".github/workflows/release-packages.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


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


def test_main_builds_artifacts_but_only_version_tag_pushes_publish():
    config = workflow()
    assert set(config["on"]) == {"push", "workflow_dispatch"}
    assert config["on"]["push"] == {"branches": ["main"], "tags": ["v*"]}
    assert config["permissions"] == {"contents": "read"}
    assert config["concurrency"]["cancel-in-progress"] == (
        "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}"
    )
    publish = config["jobs"]["publish"]
    assert publish["needs"] == ["validate", "build", "source"]
    assert set(config["on"]["workflow_dispatch"]["inputs"]) == {"ref"}
    assert publish["if"] == "github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    assert publish["permissions"] == {"contents": "write"}
    validate = config["jobs"]["validate"]
    assert validate["steps"][0]["with"]["ref"] == "${{ github.event_name == 'push' && github.sha || inputs.ref || github.sha }}"
    assert validate["outputs"]["artifact_suffix"] == "${{ steps.metadata.outputs.artifact_suffix }}"
    metadata_step = next(step for step in validate["steps"] if step.get("id") == "metadata")
    assert metadata_step["env"]["RELEASE_TAG"] == "${{ " + publish["if"] + " && github.ref_name || '' }}"
    tag_validation = next(step for step in validate["steps"] if step.get("name") == "Validate release tag target")
    assert tag_validation["if"] == publish["if"]
    assert tag_validation["env"]["EVENT_SHA"] == "${{ github.sha }}"
    website = config["jobs"]["update-website"]
    assert website["needs"] == ["validate", "publish"]
    assert website["if"] == publish["if"] + " && needs.publish.result == 'success'"
    website_step = next(step for step in website["steps"] if step.get("name") == "Generate metadata from the complete published Release and push static files")
    assert '["git", "push", "origin", "HEAD:main"]' in website_step["run"]
    assert "--tags" not in website_step["run"]
    build = config["jobs"]["build"]
    assert "if" not in build  # Main pushes and manual runs exercise all native builds.
    assert build["steps"][0]["with"]["ref"] == "${{ needs.validate.outputs.commit }}"
    assert build["strategy"]["fail-fast"] == "false"
    assert {(row["runtime"], row["os"], row["architecture"]) for row in build["strategy"]["matrix"]["include"]} == {
        ("win-x64", "windows-2022", "x64"), ("win-arm64", "windows-11-arm", "arm64"),
        ("macos-arm64", "macos-14", "arm64"), ("linux-x64", "ubuntu-24.04", "x64"),
        ("linux-arm64", "ubuntu-24.04-arm", "arm64"),
    }
    for action in ("actions/setup-python@", "actions/setup-node@"):
        setup = next(step for step in build["steps"] if step.get("uses", "").startswith(action))
        assert setup["with"]["architecture"] == "${{ matrix.architecture }}"
    assert publish["steps"][0]["with"]["ref"] == "${{ needs.validate.outputs.commit }}"
    assert publish["steps"][0]["with"]["fetch-depth"] == "0"
    names = [step.get("name") for step in publish["steps"]]
    assert names.index("Verify source, desktop and Shell packages and checksums") < names.index("Publish verified release assets")
    source = config["jobs"]["source"]
    assert source["needs"] == "validate"
    assert source["env"]["RELEASE_TAG"] == "${{ needs.validate.outputs.release_tag }}"
    assert source["steps"][0]["with"]["ref"] == "${{ needs.validate.outputs.commit }}"
    assert "if" not in source  # Main pushes and manual runs get source artifacts too.
    upload = next(step for step in source["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["with"]["name"] == (
        "ArgusAI-${{ needs.validate.outputs.version }}-source${{ needs.validate.outputs.artifact_suffix }}"
    )
    assert upload["with"]["if-no-files-found"] == "error"
    native_upload = next(step for step in build["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert native_upload["with"]["name"] == (
        "ArgusAI-${{ needs.validate.outputs.version }}-${{ matrix.runtime }}${{ needs.validate.outputs.artifact_suffix }}"
    )
    assert native_upload["with"]["if-no-files-found"] == "error"
    for job in config["jobs"].values():
        for step in job["steps"]:
            if step.get("shell") == "python":
                compile(step["run"], "workflow-inline-python", "exec")


@pytest.mark.parametrize("event, ref, release_tag", [
    ("push", "refs/heads/main", ""),
    ("workflow_dispatch", "refs/heads/main", ""),
    ("workflow_dispatch", "refs/tags/v0.1.0", ""),
    ("push", "refs/tags/v0.1.0", "v0.1.0"),
])
def test_metadata_distinguishes_branch_manual_and_tag_artifacts(monkeypatch, tmp_path, event, ref, release_tag):
    commit = "12345678" + "a" * 32
    output = tmp_path / "github-output"
    summary = tmp_path / "github-summary"
    monkeypatch.setenv("GITHUB_EVENT_NAME", event)
    monkeypatch.setenv("GITHUB_REF", ref)
    monkeypatch.setenv("RELEASE_TAG", release_tag)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        expected = [sys.executable, "scripts/bump_version.py", "--check"]
        if release_tag:
            expected += ["--tag", release_tag]
        assert command == expected
        assert kwargs["check"] is True
        return SimpleNamespace(returncode=0)

    def check_output(command, **kwargs):
        commands.append(command)
        assert kwargs["text"] is True
        if command == [sys.executable, "scripts/bump_version.py"]:
            return "0.1.0\n"
        assert command == ["git", "rev-parse", "HEAD"]
        return commit + "\n"

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(subprocess, "check_output", check_output)
    exec(python_step("validate", "Check version consistency and release tag"), {})
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert values["version"] == "0.1.0"
    assert values["commit"] == commit
    assert values["artifact_suffix"] == ("" if release_tag else "-12345678")
    assert values["release_tag"] == release_tag
    if not release_tag:
        assert all("--tag" not in command for command in commands)
    assert commit in summary.read_text(encoding="utf-8")


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
    for suffix in NATIVE_SUFFIXES:
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
    for suffix in NATIVE_SUFFIXES:
        package = tmp_path / f"ArgusAI-0.1.0-{suffix}"
        package.write_bytes(b"native-package-fixture")
        monkeypatch.setenv("PACKAGE_PATH", str(package))
        exec(python_step("build", "Verify package and create SHA-256 checksum"), {})
        packages.append(package)
    write_source_archive(tmp_path)
    if corrupt:
        packages[-1].write_bytes(b"changed-after-checksum")
        with pytest.raises(SystemExit, match="Checksum mismatch"):
            exec(python_step("publish", "Verify source, desktop and Shell packages and checksums"), {})
    else:
        exec(python_step("publish", "Verify source, desktop and Shell packages and checksums"), {})


@pytest.fixture
def source_repository(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    # Keep the fixture's object and archive bytes deterministic even when the
    # Windows runner has core.autocrlf=true in its global Git configuration.
    subprocess.run(["git", "-C", str(repository), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "core.eol", "lf"], check=True)
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
    monkeypatch.setenv("RELEASE_TAG", "v0.1.0")
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
        for name in SOURCE_FILES:
            committed = subprocess.check_output(["git", "show", f"{commit}:{name}"])
            assert archive.extractfile(f"ArgusAI-0.1.0/{name}").read() == committed
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


@pytest.mark.parametrize("release_tag", ["", "v0.1.0"], ids=["build-only", "release"])
def test_only_tag_source_archive_requires_prepared_release_notes(source_repository, monkeypatch, release_tag):
    repository, _ = source_repository
    notes = "scripts/packaging/release-notes-0.1.0.md"
    subprocess.run(["git", "rm", notes], check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Release fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "commit.gpgsign=false", "commit", "-m", "fixture pending release notes"],
                   check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    monkeypatch.setenv("SOURCE_COMMIT", commit)
    monkeypatch.setenv("RELEASE_TAG", release_tag)
    package = repository / "dist/ArgusAI-0.1.0-source.tar.gz"
    code = python_step("source", "Archive matching source and create SHA-256 checksum")
    if release_tag:
        with pytest.raises(SystemExit, match="Missing or empty tracked source file: " + notes):
            exec(code, {})
        assert not Path(str(package) + ".sha256").exists()
    else:
        exec(code, {})
        with tarfile.open(package, "r:gz") as archive:
            assert archive.pax_headers["comment"] == commit
            assert f"ArgusAI-0.1.0/{notes}" not in archive.getnames()
        assert Path(str(package) + ".sha256").is_file()


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
        exec(python_step("publish", "Verify source, desktop and Shell packages and checksums"), {})


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


@pytest.mark.parametrize("suffix", ["win-arm64-setup.exe", "linux-arm64.tar.gz"])
@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_arm_package_failures_stop_publish(monkeypatch, tmp_path, suffix, damage):
    publish_assets(tmp_path)
    package = tmp_path / f"ArgusAI-0.1.0-{suffix}"
    if damage == "missing":
        package.unlink()
    else:
        package.write_bytes(b"corrupted ARM package")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASE_VERSION", "0.1.0")
    monkeypatch.setenv("SOURCE_COMMIT", "a" * 40)
    with pytest.raises(SystemExit, match="Missing or empty|Checksum mismatch"):
        exec(python_step("publish", "Verify source, desktop and Shell packages and checksums"), {})


@pytest.mark.parametrize("platform_name, machine, node, succeeds", [
    ("win-arm64", "ARM64", "arm64", True),
    ("linux-aarch64", "aarch64", "arm64", True),
    ("win-amd64", "ARM64", "arm64", False),  # Emulated x64 Python on an ARM host.
    ("win-arm64", "ARM64", "x64", False),
])
def test_native_architecture_probe_checks_interpreter_and_node(monkeypatch, platform_name, machine, node, succeeds):
    monkeypatch.setenv("EXPECTED_ARCH", "arm64")
    def output(command, **kwargs):
        if command[0] == "node":
            return node + "\n"
        assert command[:7] == ["uv", "run", "--locked", "--group", "build", "python", "-c"]
        return json.dumps({"platform": platform_name, "machine": machine, "bits": 64})
    monkeypatch.setattr(subprocess, "check_output", output)
    code = python_step("build", "Verify native Python and Node architectures")
    if succeeds:
        exec(code, {})
    else:
        with pytest.raises(SystemExit, match="Build architecture mismatch"):
            exec(code, {})


def test_nsis_reuses_preinstalled_compiler_without_chocolatey(monkeypatch, tmp_path):
    import shutil
    compiler = tmp_path / "Program Files (x86)/NSIS/makensis.exe"
    compiler.parent.mkdir(parents=True)
    compiler.write_bytes(b"fixture compiler")
    monkeypatch.setenv("ProgramFiles(x86)", str(compiler.parents[1]))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.setenv("GITHUB_PATH", str(tmp_path / "github-path"))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))
    exec(python_step("build", "Ensure NSIS is available"), {})
    assert calls == [[str(compiler), "/VERSION"]]
    assert (tmp_path / "github-path").read_text().strip() == str(compiler.parent)


def test_windows_arm_native_dependency_preparation_uses_static_openssl_dynamic_crt(monkeypatch, tmp_path):
    import os
    import shutil
    monkeypatch.setattr(os, "environ", dict(os.environ))
    # The real ARM job exports these values before running this test suite.
    # Exercise the preparation step from a clean input environment so the
    # assertions verify every value it is responsible for exporting.
    for name in ("OPENSSL_DIR", "OPENSSL_STATIC", "OPENSSL_NO_VENDOR", "CARGO_BUILD_TARGET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    vcpkg = tmp_path / "vcpkg"
    openssl = vcpkg / "installed/arm64-windows-static-md"
    for name in ("include/openssl/ssl.h", "lib/libssl.lib", "lib/libcrypto.lib"):
        path = openssl / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "programs"))
    monkeypatch.setenv("COMSPEC", "fixture-cmd")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("VCPKG_INSTALLATION_ROOT", str(vcpkg))
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "github-env"))
    monkeypatch.setattr(shutil, "which", lambda command: None)
    calls = []
    def output(command, **kwargs):
        if command[0] == "fixture-cmd":
            wrapper = Path(command[-1])
            contents = wrapper.read_text(encoding="utf-8")
            assert 'call "' in contents
            assert "-arch=arm64 -host_arch=arm64" in contents
            return "VSCMD_ARG_HOST_ARCH=arm64\nVSCMD_ARG_TGT_ARCH=arm64\nINCLUDE=arm-headers\n"
        if command[0] == "rustc":
            return "host: aarch64-pc-windows-msvc\n"
        return str(tmp_path / "Visual Studio") + "\n"
    monkeypatch.setattr(subprocess, "check_output", output)
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append(command))
    exec(python_step("build", "Prepare Windows ARM64 native build dependencies"), {})
    triplet = (tmp_path / "build/vcpkg-triplets/arm64-windows-static-md.cmake").read_text()
    assert "VCPKG_TARGET_ARCHITECTURE arm64" in triplet
    assert "VCPKG_CRT_LINKAGE dynamic" in triplet
    assert "VCPKG_LIBRARY_LINKAGE static" in triplet
    assert any("openssl:arm64-windows-static-md" in command and "--host-triplet=arm64-windows" in command for command in calls)
    exported = (tmp_path / "github-env").read_text()
    for value in ("OPENSSL_STATIC=1", "OPENSSL_NO_VENDOR=1", "CARGO_BUILD_TARGET=aarch64-pc-windows-msvc", f"OPENSSL_DIR={openssl}"):
        assert value in exported


def test_arm_desktop_runtime_and_real_backend_probe_precede_build(monkeypatch):
    steps = workflow()["jobs"]["build"]["steps"]
    names = [step.get("name") for step in steps]
    runtime = next(step for step in steps if step.get("name") == "Install Windows ARM64 Desktop runtime")
    assert runtime["if"] == "matrix.runtime == 'win-arm64'"
    assert "-Runtime dotnet -Channel 10.0 -Architecture arm64" in runtime["run"]
    assert "-Runtime windowsdesktop -Channel 10.0 -Architecture arm64" in runtime["run"]
    assert "Test-Path -LiteralPath $dotnet -PathType Leaf" in runtime["run"]
    assert "$runtimes = & $dotnet --list-runtimes" in runtime["run"]
    assert "Microsoft.NETCore.App 10\\." in runtime["run"]
    assert "Microsoft.WindowsDesktop.App 10\\." in runtime["run"]
    assert runtime["run"].index("$runtimeExitCode = $LASTEXITCODE") > runtime["run"].index("--list-runtimes")
    assert names.index(runtime["name"]) < names.index("Install locked Python dependencies")
    assert names.index("Smoke-test Windows ARM64 WinForms backend") < names.index("Build native package")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    exec(python_step("build", "Smoke-test Windows ARM64 WinForms backend"), {})
    probe = calls[0][0][-1]
    assert probe.index("import argusai.gui.launch") < probe.index("import webview.platforms.winforms")
    assert "from System import Environment" in probe
    assert calls[0][1]["check"] is True


def test_linux_arm64_build_uses_lld_range_extension_thunks():
    build = workflow()["jobs"]["build"]
    linux_dependencies = next(
        step for step in build["steps"] if step.get("name") == "Install Linux build dependencies"
    )
    assert "lld" in linux_dependencies["run"].split()

    native_build = next(
        step for step in build["steps"] if step.get("name") == "Build native package"
    )
    assert native_build["env"]["LDFLAGS"] == (
        "${{ matrix.runtime == 'linux-arm64' && '-fuse-ld=lld' || '' }}"
    )


@pytest.mark.parametrize("arch, expected", [("x64", ["netfx", "coreclr"]), ("arm64", ["coreclr"])])
def test_packaged_gui_probe_loads_each_supported_clr_before_upload(monkeypatch, tmp_path, arch, expected):
    steps = workflow()["jobs"]["build"]["steps"]
    names = [step.get("name") for step in steps]
    probe_name = "Smoke-test packaged Windows GUI backends"
    probe = next(step for step in steps if step.get("name") == probe_name)
    assert probe["if"] == "runner.os == 'Windows'"
    assert names.index("Build native package") < names.index(probe_name) < names.index("Upload package and checksum")
    installer = next(step for step in steps if step.get("name") == "Install Windows x64 Desktop runtime for GUI verification")
    assert installer["if"] == "matrix.runtime == 'win-x64'"
    assert "-Runtime dotnet -Channel 10.0 -Architecture x64" in installer["run"]
    assert "-Runtime windowsdesktop -Channel 10.0 -Architecture x64" in installer["run"]
    assert names.index(installer["name"]) < names.index("Install locked Python dependencies")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXPECTED_ARCH", arch)
    monkeypatch.setenv("PYTHONNET_CORECLR_RUNTIME_CONFIG", "outside-user-config.json")
    executable = tmp_path / "dist/ArgusAI.dist/ArgusAI.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"packaged fixture")
    calls = []

    def run(command, **kwargs):
        assert command == [str(executable), "--self-check-gui"]
        assert "PYTHONNET_CORECLR_RUNTIME_CONFIG" not in kwargs["env"]
        assert kwargs["timeout"] == 120
        runtime = kwargs["env"]["PYTHONNET_RUNTIME"]
        calls.append(runtime)
        directory = executable.parent / "webview/lib"
        if runtime == "netfx":
            directory /= "net462"
        directory.mkdir(parents=True, exist_ok=True)
        dlls = {}
        for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
            path = directory / name
            path.write_bytes(b"assembly fixture")
            dlls[name] = str(path)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "runtime": runtime, "dlls": dlls}), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    exec(probe["run"], {})
    assert calls == expected


@pytest.mark.parametrize("system, expected_platform, backend", [("Linux", "linux", "gtkwebkit2"), ("Darwin", "darwin", "wkwebview")])
@pytest.mark.parametrize("packaged", [False, True])
def test_unix_gui_smoke_loads_real_platform_backend_before_publication(monkeypatch, tmp_path, system, expected_platform, backend, packaged):
    import platform
    import plistlib

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setenv("GI_TYPELIB_PATH", "/outside/runner/typelibs")
    steps = workflow()["jobs"]["build"]["steps"]
    names = [step.get("name") for step in steps]
    step_name = "Smoke-test packaged Unix GUI backends" if packaged else "Smoke-test source Unix GUI backends"
    probe = next(step for step in steps if step.get("name") == step_name)
    assert probe["if"] == "runner.os != 'Windows'"
    if packaged:
        assert names.index("Build native package") < names.index(step_name) < names.index("Upload package and checksum")
    else:
        assert names.index("Install locked Python dependencies") < names.index(step_name) < names.index("Build native package")
    info = tmp_path / "dist/ArgusAI.app/Contents/Info.plist"
    info.parent.mkdir(parents=True)
    info.write_bytes(plistlib.dumps({"CFBundleExecutable": "ArgusAI"}))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "codesign":
            assert system == "Darwin" and packaged
            assert command[1:4] == ["--verify", "--deep", "--strict"]
            return SimpleNamespace(returncode=0)
        assert command[-1] == "--self-check-gui"
        if system == "Linux":
            assert command[:4] == ["dbus-run-session", "--", "sh", "-c"]
            assert command[4] == 'exec 9>&2; exec xvfb-run -a sh -c \'exec 2>&9 9>&-; exec "$@"\' -- "$@"'
            assert command[5] == "--"
            if packaged:
                assert "GI_TYPELIB_PATH" not in kwargs["env"]
        if not packaged:
            assert command[-9:] == ["uv", "run", "--locked", "--group", "build", "python", "-m", "argusai", "--self-check-gui"]
        report = {"ok": True, "platform": expected_platform, "backend": backend}
        return SimpleNamespace(returncode=0, stdout=json.dumps(report), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    exec(probe["run"], {})
    assert len(calls) == (2 if packaged and system == "Darwin" else 1)


@pytest.mark.skipif(sys.platform != "linux" or not shutil.which("dbus-run-session") or not shutil.which("xvfb-run"),
                    reason="requires native Linux headless wrappers")
@pytest.mark.parametrize("packaged", [False, True])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_linux_gui_wrapper_preserves_real_stderr_exit_and_literal_arguments(monkeypatch, tmp_path, capsys, packaged, exit_code):
    import platform

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    actual_run = subprocess.run
    arguments = ["path with spaces", "; echo must-not-execute", "$(must-not-execute)", "quote'\"value"]
    script = (
        "import json,os,sys\n"
        "os.write(2,b'native GUI diagnostic\\n')\n"
        "print(json.dumps({'ok':True,'platform':'linux','backend':'gtkwebkit2','argv':sys.argv[1:]}))\n"
        f"raise SystemExit({exit_code})\n"
    )
    results = []

    def run(command, **kwargs):
        assert command[5] == "--"
        result = actual_run([*command[:6], sys.executable, "-c", script, *arguments], **kwargs)
        assert result.returncode == exit_code
        assert json.loads(result.stdout)["argv"] == arguments
        assert "native GUI diagnostic" in result.stderr
        assert "native GUI diagnostic" not in result.stdout
        results.append(result)
        return result

    monkeypatch.setattr(subprocess, "run", run)
    step = "Smoke-test packaged Unix GUI backends" if packaged else "Smoke-test source Unix GUI backends"
    if exit_code:
        with pytest.raises(SystemExit, match="native GUI diagnostic"):
            exec(python_step("build", step), {})
    else:
        exec(python_step("build", step), {})
        assert "native GUI diagnostic" in capsys.readouterr().err
    assert len(results) == 1


@pytest.mark.parametrize("packaged", [False, True])
@pytest.mark.parametrize("failure", ["native_load", "reported_failure", "wrong_platform", "wrong_backend"])
def test_unix_gui_smoke_blocks_dependency_or_backend_failures(monkeypatch, tmp_path, packaged, failure):
    import platform

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    report = {"ok": failure != "reported_failure", "platform": "darwin" if failure == "wrong_platform" else "linux",
              "backend": "qt" if failure == "wrong_backend" else "gtkwebkit2"}
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=1 if failure == "native_load" else 0, stdout=json.dumps(report), stderr="missing GTK fixture"))
    name = "Smoke-test packaged Unix GUI backends" if packaged else "Smoke-test source Unix GUI backends"
    with pytest.raises(SystemExit):
        exec(python_step("build", name), {})


def test_linux_build_declares_gi_typelibs_and_headless_test_dependencies():
    dependencies = next(step["run"] for step in workflow()["jobs"]["build"]["steps"]
                        if step.get("name") == "Install Linux build dependencies")
    for package in ("libgirepository1.0-dev", "libcairo2-dev", "libffi-dev", "python3-dev",
                    "gir1.2-gtk-3.0", "gir1.2-webkit2-4.1", "gir1.2-soup-3.0", "xvfb", "xauth", "dbus-x11"):
        assert package in dependencies.split()


@pytest.mark.parametrize("packaged", [False, True])
def test_unix_gui_smoke_preserves_invalid_protocol_output_for_diagnosis(monkeypatch, tmp_path, packaged):
    import platform

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=0, stdout='native stdout fixture\n{"ok": true}', stderr="native stderr fixture"))
    name = "Smoke-test packaged Unix GUI backends" if packaged else "Smoke-test source Unix GUI backends"
    with pytest.raises(SystemExit) as failure:
        exec(python_step("build", name), {})
    assert "invalid JSON" in str(failure.value)
    assert "native stdout fixture" in str(failure.value)
    assert "native stderr fixture" in str(failure.value)


@pytest.mark.parametrize("failure", ["load_error", "wrong_runtime", "missing_assembly", "outside_package", "wrong_variant", "empty_dll"])
def test_packaged_gui_probe_blocks_unusable_or_external_assemblies(monkeypatch, tmp_path, failure):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXPECTED_ARCH", "x64")
    root = tmp_path / "dist/ArgusAI.dist"
    root.mkdir(parents=True)

    def run(_command, **_kwargs):
        directory = tmp_path / "global-sdk" if failure == "outside_package" else root / "webview/lib/net462"
        if failure == "wrong_variant":
            directory = root / "webview/lib"
        directory.mkdir(parents=True, exist_ok=True)
        dlls = {}
        for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll"):
            path = directory / name
            path.write_bytes(b"" if failure == "empty_dll" else b"assembly fixture")
            dlls[name] = str(path)
        if failure == "missing_assembly":
            dlls.pop("Microsoft.Web.WebView2.WinForms.dll")
        report = {"ok": True, "runtime": "coreclr" if failure == "wrong_runtime" else "netfx", "dlls": dlls}
        return SimpleNamespace(returncode=1 if failure == "load_error" else 0,
                               stdout=json.dumps(report), stderr="Missing SDK fixture" if failure == "load_error" else "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit):
        exec(python_step("build", "Smoke-test packaged Windows GUI backends"), {})


@pytest.mark.parametrize("present, same", [(False, True), (True, True), (True, False)])
def test_tag_push_preflight_requires_existing_tag_and_validated_target(source_repository, monkeypatch, present, same):
    _, commit = source_repository
    if present:
        subprocess.run(["git", "tag", "v0.1.0", commit], check=True)
    monkeypatch.setenv("EVENT_NAME", "push")
    monkeypatch.setenv("EVENT_REF", "refs/tags/v0.1.0")
    monkeypatch.setenv("EVENT_SHA", commit)
    monkeypatch.setenv("RELEASE_TAG", "v0.1.0")
    monkeypatch.setenv("SOURCE_COMMIT", commit if same else "b" * 40)
    code = python_step("validate", "Validate release tag target")
    if present and same:
        exec(code, {})
    else:
        with pytest.raises(SystemExit, match="Release tag does not match" if present else "release tag is missing"):
            exec(code, {})


@pytest.mark.parametrize("event, ref", [
    ("workflow_dispatch", "refs/heads/main"),
    ("workflow_dispatch", "refs/tags/v0.1.0"),
    ("release", "refs/tags/v0.1.0"),
    ("push", "refs/heads/main"),
])
def test_manual_release_and_branch_events_cannot_execute_release_mutation(monkeypatch, event, ref):
    monkeypatch.setenv("EVENT_NAME", event)
    monkeypatch.setenv("EVENT_REF", ref)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("No command should run"))
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: pytest.fail("No command should run"))
    with pytest.raises(SystemExit, match="Only version tag pushes"):
        exec(python_step("publish", "Publish verified release assets"), {})


@pytest.fixture
def tag_push(source_repository, monkeypatch):
    repository, commit = source_repository
    subprocess.run(["git", "tag", "v0.1.0", commit], check=True)
    monkeypatch.setenv("EVENT_NAME", "push")
    monkeypatch.setenv("EVENT_REF", "refs/tags/v0.1.0")
    monkeypatch.setenv("EVENT_SHA", commit)
    monkeypatch.setenv("RELEASE_TAG", "v0.1.0")
    return repository, commit


@pytest.mark.parametrize("existing", [False, True])
def test_tag_push_creates_with_tracked_notes_or_uploads_matching_existing_release(tag_push, monkeypatch, existing):
    repository, commit = tag_push
    assets = repository / "release-assets"
    assets.mkdir()
    publish_assets(assets)
    notes = (repository / "scripts/packaging/release-notes-0.1.0.md").read_text(encoding="utf-8")
    real_run = subprocess.run
    calls = []
    def run(command, **kwargs):
        if command[0] != "gh":
            return real_run(command, **kwargs)
        calls.append(command)
        if command[1:3] == ["release", "view"]:
            return SimpleNamespace(returncode=0 if existing else 1, stdout=json.dumps({"tagName": "v0.1.0", "isDraft": False, "body": notes}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    exec(python_step("publish", "Publish verified release assets"), {})
    mutation = calls[-1]
    assert mutation[1:4] == ["release", "upload" if existing else "create", "v0.1.0"]
    assert len([value for value in mutation if Path(value).parent.name == "release-assets"]) == 22
    if existing:
        assert "--clobber" in mutation
    else:
        assert "--verify-tag" in mutation
        assert mutation[mutation.index("--target") + 1] == commit
        assert mutation[mutation.index("--title") + 1] == "ArgusAI v0.1.0"
        assert Path(mutation[mutation.index("--notes-file") + 1]).as_posix() == "scripts/packaging/release-notes-0.1.0.md"


@pytest.mark.parametrize("step", [("validate", "Validate release tag target"), ("publish", "Publish verified release assets")])
def test_tag_push_rejects_source_sha_different_from_the_event(tag_push, monkeypatch, step):
    monkeypatch.setenv("EVENT_SHA", "b" * 40)
    with pytest.raises(SystemExit, match="Tag push source does not match"):
        exec(python_step(*step), {})


@pytest.mark.parametrize(
    "damage, error",
    [("missing-tag", "release tag is missing"), ("wrong-version", "tag does not match validated version"),
     ("wrong-checkout", "checkout does not match validated source commit")],
)
def test_tag_publish_revalidates_identity_before_any_release_commands(tag_push, monkeypatch, damage, error):
    if damage == "missing-tag":
        subprocess.run(["git", "tag", "-d", "v0.1.0"], check=True, capture_output=True)
    elif damage == "wrong-version":
        monkeypatch.setenv("RELEASE_VERSION", "9.9.9")
    else:
        monkeypatch.setenv("SOURCE_COMMIT", "b" * 40)
    real_run = subprocess.run

    def run(command, **kwargs):
        assert command[0] != "gh", "invalid release identity must prevent Release API commands"
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit, match=error):
        exec(python_step("publish", "Publish verified release assets"), {})


def test_tag_push_preflight_accepts_an_annotated_tag_object(source_repository, monkeypatch):
    _, commit = source_repository
    subprocess.run(["git", "-c", "user.name=Release fixture", "-c", "user.email=fixture@example.invalid",
                    "-c", "tag.gpgsign=false", "tag", "-a", "v0.1.0", "-m", "version tag", commit], check=True)
    tag_object = subprocess.check_output(["git", "rev-parse", "v0.1.0"], text=True).strip()
    assert tag_object != commit
    monkeypatch.setenv("EVENT_NAME", "push")
    monkeypatch.setenv("EVENT_REF", "refs/tags/v0.1.0")
    monkeypatch.setenv("EVENT_SHA", tag_object)
    monkeypatch.setenv("RELEASE_TAG", "v0.1.0")
    exec(python_step("validate", "Validate release tag target"), {})


@pytest.mark.parametrize(
    "damage, error",
    [({"tagName": "v9.9.9"}, "unexpected tag"), ({"isDraft": True}, "draft"), ({"body": "wrong notes"}, "release notes do not match")],
)
def test_tag_push_refuses_wrong_existing_release_metadata(tag_push, monkeypatch, damage, error):
    repository, _ = tag_push
    assets = repository / "release-assets"
    assets.mkdir()
    publish_assets(assets)
    notes = (repository / "scripts/packaging/release-notes-0.1.0.md").read_text(encoding="utf-8")
    real_run = subprocess.run
    calls = []

    def run(command, **kwargs):
        if command[0] != "gh":
            return real_run(command, **kwargs)
        calls.append(command)
        assert command[1:3] == ["release", "view"], "invalid Release metadata must prevent mutations"
        return SimpleNamespace(returncode=0, stdout=json.dumps({"tagName": "v0.1.0", "isDraft": False, "body": notes, **damage}))

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit, match=error):
        exec(python_step("publish", "Publish verified release assets"), {})
    assert len(calls) == 1


def test_tag_push_rejects_uncommitted_release_notes_before_gh_commands(tag_push, monkeypatch):
    repository, _ = tag_push
    notes = repository / "scripts/packaging/release-notes-0.1.0.md"
    notes.write_text("uncommitted replacement", encoding="utf-8")
    real_run = subprocess.run

    def run(command, **kwargs):
        assert command[0] != "gh", "release mutations must not run with uncommitted notes"
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit, match="Release notes do not match"):
        exec(python_step("publish", "Publish verified release assets"), {})


def test_main_preview_never_updates_latest_or_the_stable_feed():
    config = workflow()
    preview = config["jobs"]["publish-preview"]
    assert preview["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert preview["needs"] == ["validate", "build", "source"]
    assert preview["permissions"] == {"contents": "write"}
    assert "publish-preview" not in config["jobs"]["update-website"]["needs"]
    assert python_step("publish-preview", "Verify source, desktop and Shell packages and checksums") == python_step("publish", "Verify source, desktop and Shell packages and checksums")


@pytest.mark.parametrize("existing", [False, True])
def test_main_preview_marks_prerelease_and_not_latest(source_repository, monkeypatch, existing):
    repository, commit = source_repository
    monkeypatch.chdir(repository)
    assets = repository / "release-assets"
    assets.mkdir()
    publish_assets(assets)
    monkeypatch.setenv("EVENT_NAME", "push")
    monkeypatch.setenv("EVENT_REF", "refs/heads/main")
    monkeypatch.setenv("SOURCE_COMMIT", commit)
    monkeypatch.setenv("RELEASE_VERSION", "0.1.0")
    monkeypatch.setenv("GITHUB_REPOSITORY", "fixture/repository")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(repository / "summary.md"))
    tag = "preview-" + commit[:12]
    real_run = subprocess.run
    calls = []
    def run(command, **kwargs):
        if command[0] != "gh":
            return real_run(command, **kwargs)
        calls.append(command)
        if command[1] == "api":
            return SimpleNamespace(returncode=0 if existing else 1, stdout=json.dumps({"object": {"type":"commit", "sha":commit}}), stderr="" if existing else "HTTP 404")
        if command[1:3] == ["release", "view"]:
            return SimpleNamespace(returncode=0 if existing else 1, stdout=json.dumps({"tagName":tag, "isDraft":False, "isPrerelease":True}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    exec(python_step("publish-preview", "Publish verified preview assets"), {})
    publication = next(call for call in calls if call[1:3] == ["release", "edit" if existing else "create"])
    assert "--prerelease" in publication and "--latest=false" in publication
    assert publication[3] == tag and not tag.startswith("v")
    if not existing:
        assert publication[publication.index("--target")+1] == commit
    assert len(list(assets.iterdir())) == 22
    for package in assets.iterdir():
        assert tag in package.name
        if not package.name.endswith(".sha256"):
            assert Path(str(package)+".sha256").read_text().strip() == hashlib.sha256(package.read_bytes()).hexdigest()+"  "+package.name


@pytest.mark.parametrize("ref", ["refs/tags/v0.1.0", "refs/heads/develop"])
def test_preview_refuses_non_main_push_before_any_release_write(monkeypatch, tmp_path, ref):
    monkeypatch.setenv("EVENT_NAME", "push")
    monkeypatch.setenv("EVENT_REF", ref)
    calls=[]
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append(a))
    with pytest.raises(SystemExit, match="requires a main push"):
        exec(python_step("publish-preview", "Publish verified preview assets"), {})
    assert not calls


def test_preview_verification_still_checks_source_without_formal_notes(monkeypatch, tmp_path):
    publish_assets(tmp_path)
    write_source_archive(tmp_path, omit="scripts/packaging/release-notes-0.1.0.md")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASE_VERSION", "0.1.0")
    monkeypatch.setenv("SOURCE_COMMIT", "a"*40)
    monkeypatch.setenv("PREVIEW_BUILD", "1")
    exec(python_step("publish-preview", "Verify source, desktop and Shell packages and checksums"), {})
    package = tmp_path/"ArgusAI-0.1.0-win-arm64-setup.exe"
    package.write_bytes(b"corrupted")
    with pytest.raises(SystemExit, match="Checksum mismatch"):
        exec(python_step("publish-preview", "Verify source, desktop and Shell packages and checksums"), {})


@pytest.mark.parametrize("damage", ["formal-release", "wrong-tag-target", "tag-lookup-failed"])
def test_preview_refuses_existing_formal_release_or_unverified_tag(source_repository, monkeypatch, damage):
    repository, commit = source_repository
    monkeypatch.chdir(repository)
    assets = repository / "release-assets"
    assets.mkdir()
    publish_assets(assets)
    for key,value in {"EVENT_NAME":"push", "EVENT_REF":"refs/heads/main", "SOURCE_COMMIT":commit,
                      "RELEASE_VERSION":"0.1.0", "GITHUB_REPOSITORY":"fixture/repository"}.items():
        monkeypatch.setenv(key,value)
    calls=[]
    real_run=subprocess.run
    def run(command, **kwargs):
        if command[0]!="gh": return real_run(command, **kwargs)
        calls.append(command)
        if command[1]=="api":
            return SimpleNamespace(returncode=1 if damage=="tag-lookup-failed" else 0,
                                   stdout=json.dumps({"object":{"type":"commit", "sha":"b"*40 if damage=="wrong-tag-target" else commit}}),
                                   stderr="HTTP 503" if damage=="tag-lookup-failed" else "")
        return SimpleNamespace(returncode=0,stdout=json.dumps({"tagName":"preview-"+commit[:12], "isDraft":False,"isPrerelease":False}))
    monkeypatch.setattr(subprocess,"run",run)
    with pytest.raises(SystemExit):
        exec(python_step("publish-preview", "Publish verified preview assets"),{})
    assert not any(call[1:3] in (["release","edit"],["release","create"],["release","upload"]) for call in calls)
