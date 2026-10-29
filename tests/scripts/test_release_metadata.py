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
NATIVE_SUFFIXES = ("win-x64-setup.exe", "win-arm64-setup.exe", "macos-arm64.dmg", "linux-x64.tar.gz", "linux-arm64.tar.gz")
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


def test_release_waits_for_all_platforms_and_manual_publish_is_explicit():
    config = workflow()
    assert config["on"]["release"]["types"] == ["published"]
    assert config["permissions"] == {"contents": "read"}
    publish = config["jobs"]["publish"]
    assert publish["needs"] == ["validate", "build", "source"]
    assert config["on"]["workflow_dispatch"]["inputs"]["publish_release"] == {
        "description": "Publish the verified packages as a Release after every build succeeds.",
        "required": "false", "default": "false", "type": "boolean",
    }
    assert publish["if"] == "github.event_name == 'release' || (github.event_name == 'workflow_dispatch' && inputs.publish_release)"
    assert publish["permissions"] == {"contents": "write"}
    build = config["jobs"]["build"]
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
    assert names.index("Verify source, all five packages and checksums") < names.index("Publish verified release assets")
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
            exec(python_step("publish", "Verify source, all five packages and checksums"), {})
    else:
        exec(python_step("publish", "Verify source, all five packages and checksums"), {})


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
        exec(python_step("publish", "Verify source, all five packages and checksums"), {})


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
        exec(python_step("publish", "Verify source, all five packages and checksums"), {})


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
    monkeypatch.chdir(tmp_path)
    vcpkg = tmp_path / "vcpkg"
    openssl = vcpkg / "installed/arm64-windows-static-md"
    for name in ("include/openssl/ssl.h", "lib/libssl.lib", "lib/libcrypto.lib"):
        path = openssl / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "programs"))
    monkeypatch.setenv("COMSPEC", "fixture-cmd")
    monkeypatch.setenv("VCPKG_INSTALLATION_ROOT", str(vcpkg))
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "github-env"))
    monkeypatch.setattr(shutil, "which", lambda command: None)
    calls = []
    def output(command, **kwargs):
        if command[0] == "fixture-cmd":
            assert "-arch=arm64 -host_arch=arm64" in command[-1]
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
    assert "-Runtime windowsdesktop -Channel 10.0 -Architecture arm64" in runtime["run"]
    assert names.index(runtime["name"]) < names.index("Install locked Python dependencies")
    assert names.index("Smoke-test Windows ARM64 WinForms backend") < names.index("Build native package")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    exec(python_step("build", "Smoke-test Windows ARM64 WinForms backend"), {})
    probe = calls[0][0][-1]
    assert probe.index("import argusai.gui.launch") < probe.index("import webview.platforms.winforms")
    assert "from System import Environment" in probe
    assert calls[0][1]["check"] is True


@pytest.mark.parametrize("present, same", [(False, True), (True, True), (True, False)])
def test_manual_preflight_allows_new_tag_but_rejects_wrong_existing_target(source_repository, monkeypatch, present, same):
    _, commit = source_repository
    if present:
        subprocess.run(["git", "tag", "v0.1.0", commit], check=True)
    monkeypatch.setenv("EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("RELEASE_TAG", "v0.1.0")
    monkeypatch.setenv("SOURCE_COMMIT", commit if same else "b" * 40)
    code = python_step("validate", "Validate release tag target")
    if same:
        exec(code, {})
    else:
        with pytest.raises(SystemExit, match="Release tag does not match"):
            exec(code, {})


def test_default_manual_run_cannot_execute_release_mutation(monkeypatch):
    monkeypatch.setenv("EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("MANUAL_PUBLISH", "false")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("No command should run"))
    with pytest.raises(SystemExit, match="Publishing was not requested"):
        exec(python_step("publish", "Publish verified release assets"), {})


@pytest.mark.parametrize("existing", [False, True])
def test_explicit_manual_publish_creates_with_tracked_notes_or_uploads_existing(source_repository, monkeypatch, existing):
    repository, commit = source_repository
    assets = repository / "release-assets"
    assets.mkdir()
    publish_assets(assets)
    if existing:
        subprocess.run(["git", "tag", "v0.1.0", commit], check=True)
    monkeypatch.setenv("EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("MANUAL_PUBLISH", "true")
    monkeypatch.setenv("RELEASE_TAG", "v0.1.0")
    real_run = subprocess.run
    calls = []
    def run(command, **kwargs):
        if command[0] != "gh":
            return real_run(command, **kwargs)
        calls.append(command)
        if command[1:3] == ["release", "view"]:
            return SimpleNamespace(returncode=0 if existing else 1, stdout=json.dumps({"tagName": "v0.1.0", "isDraft": False}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, "run", run)
    exec(python_step("publish", "Publish verified release assets"), {})
    mutation = calls[-1]
    assert mutation[1:4] == ["release", "upload" if existing else "create", "v0.1.0"]
    assert len([value for value in mutation if Path(value).parent.name == "release-assets"]) == 12
    if existing:
        assert "--clobber" in mutation
    else:
        assert mutation[mutation.index("--target") + 1] == commit
        assert mutation[mutation.index("--title") + 1] == "ArgusAI v0.1.0"
        assert Path(mutation[mutation.index("--notes-file") + 1]).as_posix() == "scripts/packaging/release-notes-0.1.0.md"
