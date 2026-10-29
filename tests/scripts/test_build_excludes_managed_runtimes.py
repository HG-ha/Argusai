from __future__ import annotations

import importlib.util
import json
import plistlib
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("argusai_release_build", REPO / "build.py")
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


def options(monkeypatch, system="Linux", *extra, machine="x86_64", python_platform=None):
    monkeypatch.setattr(build.platform, "machine", lambda: machine)
    monkeypatch.setattr(build.sysconfig, "get_platform", lambda: python_platform or (
        "win-arm64" if machine.lower() in {"arm64", "aarch64"} else "win-amd64"
    ))
    return build.parse_args(list(extra), system=system)


@pytest.mark.parametrize("system,mode,tag", [("Windows", "folder", "win-x64"), ("Darwin", "app", "macos-x64"), ("Linux", "folder", "linux-x64")])
def test_native_defaults(monkeypatch, system, mode, tag):
    args = options(monkeypatch, system)
    assert (args.mode, args.runtime_arch) == (mode, tag)
    flags = build.nuitka_args(args, win32_helper=Path("webview/platforms/win32.py"))
    assert "--include-package=websockets" in flags
    assert "--include-package=lark_oapi.core" in flags
    assert "--include-package=lark_oapi.ws" in flags
    assert "--include-package=lark_oapi" not in flags
    assert "--nofollow-import-to=lark_oapi.api.*" in flags
    assert "--include-data-files=argusai/core/reverse/ghidra_scripts/*.py=argusai/core/reverse/ghidra_scripts/" in flags
    node = "node.exe" if system == "Windows" else "node"
    assert f"--noinclude-dlls=playwright/driver/{node}" in flags
    assert "--noinclude-data-files=argusai/webui/node_modules/*" in flags
    assert "--noinclude-data-files=argusai/webui/src/*" in flags
    assert not any(f"vendor/{name}" in flag for name in build.REMOVED_RUNTIMES for flag in flags if flag.startswith("--include"))
    if system == "Windows":
        assert "--windows-console-mode=hide" in flags
        assert f"--include-data-files={Path("webview/platforms/win32.py")}=webview/platforms/win32.py" in flags
    if system == "Darwin":
        assert "--macos-create-app-bundle" in flags
        assert not any("include-data-dir=argusai/vendor" in flag for flag in flags)
        assert not any("include-raw-dir=argusai/vendor" in flag for flag in flags)
    else:
        assert "--include-raw-dir=argusai/vendor/sqlmap=argusai/vendor/sqlmap" in flags
        assert "--include-data-dir=argusai/vendor/sqlmap=argusai/vendor/sqlmap" not in flags


@pytest.mark.parametrize("system,argv", [
    ("Linux", ["--dmg"]), ("Darwin", ["--nsis"]), ("Windows", ["--mode", "app"]),
    ("Darwin", ["--dmg", "--onefile"]), ("Darwin", ["--notarize"]),
    ("Linux", ["--jobs", "-1"]), ("Linux", ["--app-name", "../escape"]),
    ("Linux", ["--output-dir", "."]), ("Linux", ["--output-dir", "argusai/out"]),
    ("Linux", ["--output-dir", ".git/out"]), ("Linux", ["--arch", "arm64"]),
])
def test_invalid_builds_fail_before_side_effects(monkeypatch, system, argv):
    with pytest.raises(SystemExit):
        options(monkeypatch, system, *argv)


def test_compiler_controls(monkeypatch):
    args = options(monkeypatch, "Windows", "--onefile", "--jobs", "2", "--fast", "--console")
    flags = build.nuitka_args(args)
    assert "--onefile" in flags
    assert "--jobs=2" in flags
    assert "--onefile-no-compression" in flags
    assert "--windows-console-mode=force" in flags
    assert "--onefile-tempdir-spec={CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION}" in flags


@pytest.mark.parametrize("system,machine,runtime", [
    ("Windows", "ARM64", "win-arm64"),
    ("Linux", "aarch64", "linux-arm64"),
])
def test_native_arm64_build_plan_uses_matching_abi(monkeypatch, system, machine, runtime):
    args = options(monkeypatch, system, "--arch", "arm64", machine=machine)
    assert args.target_arch == args.python_arch == "arm64"
    assert args.runtime_arch == runtime
    flags = build.nuitka_args(args)
    assert ("--msvc=latest" in flags) == (system == "Windows")
    runtime_flag = f"--include-data-files={build.WINDOWS_ARM_RUNTIME_CONFIG}={build.WINDOWS_ARM_RUNTIME_CONFIG}"
    assert (runtime_flag in flags) == (system == "Windows")
    assert "--mingw64" not in flags
    assert not any("innoextract/win-x64" in flag or "innoextract/linux-x64" in flag for flag in flags)


def test_windows_arm64_host_does_not_disguise_x64_python(monkeypatch):
    with pytest.raises(SystemExit):
        options(monkeypatch, "Windows", "--arch", "arm64", machine="ARM64", python_platform="win-amd64")
    args = options(monkeypatch, "Windows", machine="ARM64", python_platform="win-amd64")
    assert args.runtime_arch == "win-x64"
    assert args.python_arch == "x86_64"


def test_windows_32_bit_interpreter_is_rejected(monkeypatch):
    with pytest.raises(SystemExit):
        options(monkeypatch, "Windows", python_platform="win32")


@pytest.mark.parametrize("system", ["Windows", "Linux"])
def test_arm64_preparation_does_not_download_x64_vendor(monkeypatch, tmp_path, capsys, system):
    args = options(monkeypatch, system, "--skip-webui", machine="ARM64")
    args.output = tmp_path / "output"
    source = tmp_path / "source"
    monkeypatch.setattr(build, "ROOT", source)
    index = source / "argusai/webui/dist/index.html"
    index.parent.mkdir(parents=True)
    index.write_text("frontend fixture")
    from scripts import frontend_notices
    monkeypatch.setattr(frontend_notices, "check_notices", lambda path: None)
    monkeypatch.setattr(build, "prepare_legal", lambda args: None)
    monkeypatch.setattr(build.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(build.shutil, "which", lambda name: f"/tools/{name}")
    monkeypatch.setattr(build, "run", lambda *a, **kw: pytest.fail("ARM64 downloaded an unsupported vendor archive"))
    helper = source / "webview/platforms/win32.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("helper fixture")
    runtime_config = source / build.WINDOWS_ARM_RUNTIME_CONFIG
    runtime_config.parent.mkdir(parents=True, exist_ok=True)
    runtime_config.write_text('{"runtimeOptions": {}}')
    monkeypatch.setitem(build.sys.modules, "webview", SimpleNamespace(__file__=str(source / "webview/__init__.py")))
    result = build.prepare(args)
    assert result == (helper if system == "Windows" else None)
    assert args.output.is_dir()
    assert "no official portable ARM64 archive" in capsys.readouterr().out


def test_windows_arm64_preparation_requires_runtime_config(monkeypatch, tmp_path):
    args = options(monkeypatch, "Windows", machine="ARM64")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="Windows ARM64 .NET runtime configuration"):
        build.prepare(args)


def test_windows_nsis_requirement_is_checked_before_compilation(monkeypatch, tmp_path):
    args = options(monkeypatch, "Windows", "--nsis", "--skip-webui")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    monkeypatch.setattr(build.shutil, "which", lambda name: None)
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "program-files-x86"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "program-files"))
    with pytest.raises(ValueError, match="Install NSIS 3"):
        build.prepare(args)


def populate_payload(path, args):
    for name in ["argusai/webui/dist/index.html", "argusai/webui/dist/THIRD_PARTY_NOTICES.txt", "argusai/vendor/sqlmap/sqlmap.py", *(f"argusai/core/reverse/ghidra_scripts/{name}" for name in build.GHIDRA_SCRIPTS), *(f"argusai/legal/{name}" for name in build.LEGAL_FILES)]:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture")
    (path / "argusai/vendor/runners").mkdir(parents=True)


def test_payload_rejects_missing_resources_and_bundled_runtime(monkeypatch, tmp_path):
    args = options(monkeypatch)
    monkeypatch.setattr(build, "ROOT", tmp_path / "source")
    with pytest.raises(ValueError, match="Missing packaged"):
        build.verify_payload(tmp_path, args)
    populate_payload(tmp_path, args)
    build.verify_payload(tmp_path, args)
    forbidden = tmp_path / "playwright/driver/node"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_text("excluded binary")
    with pytest.raises(ValueError, match="excluded runtimes"):
        build.verify_payload(tmp_path, args)


def test_payload_requires_frontend_notices(monkeypatch, tmp_path):
    args = options(monkeypatch)
    monkeypatch.setattr(build, "ROOT", tmp_path / "source")
    populate_payload(tmp_path, args)
    (tmp_path / "argusai/webui/dist/THIRD_PARTY_NOTICES.txt").unlink()
    with pytest.raises(ValueError, match="THIRD_PARTY_NOTICES.txt"):
        build.verify_payload(tmp_path, args)


def test_payload_requires_gpl_and_dependency_notices(monkeypatch, tmp_path):
    args = options(monkeypatch)
    monkeypatch.setattr(build, "ROOT", tmp_path / "source")
    populate_payload(tmp_path, args)
    (tmp_path / "argusai/legal/python/manifest.json").unlink()
    with pytest.raises(ValueError, match="manifest.json"):
        build.verify_payload(tmp_path, args)


def test_windows_arm64_payload_requires_coreclr_runtime_config(monkeypatch, tmp_path):
    args = options(monkeypatch, "Windows", machine="ARM64")
    monkeypatch.setattr(build, "ROOT", tmp_path / "source")
    populate_payload(tmp_path, args)
    with pytest.raises(ValueError, match="windows-arm64.runtimeconfig.json"):
        build.verify_payload(tmp_path, args)
    runtime_config = tmp_path / build.WINDOWS_ARM_RUNTIME_CONFIG
    runtime_config.parent.mkdir(parents=True, exist_ok=True)
    runtime_config.write_text('{"runtimeOptions": {}}')
    build.verify_payload(tmp_path, args)


@pytest.mark.parametrize("mode", ["folder", "onefile"])
@pytest.mark.parametrize("machine,runtime", [("x86_64", "linux-x64"), ("aarch64", "linux-arm64")])
def test_linux_archive_and_onefile_cleanup(monkeypatch, tmp_path, mode, machine, runtime):
    args = options(monkeypatch, "Linux", "--mode", mode, machine=machine)
    args.output = tmp_path
    args.app_name = "ReleaseTool"
    monkeypatch.setattr(build, "ROOT", tmp_path / "source")
    dist = tmp_path / "__main__.dist"
    populate_payload(dist, args)
    binary = (tmp_path if mode == "onefile" else dist) / args.app_name
    binary.write_text("native executable fixture")
    binary.chmod(0o755)
    for suffix in (".build", ".onefile-build"):
        (tmp_path / ("__main__" + suffix)).mkdir()
    archive = build.finish(args)
    with tarfile.open(archive) as stream:
        members = stream.getnames()
        expected = f"{args.app_name}-{args.version}-{runtime}/{args.app_name}"
        assert expected in members
        assert stream.getmember(expected).mode & 0o111
        assert f"{args.app_name}-{args.version}-{runtime}/licenses/LICENSE" in members
        assert f"{args.app_name}-{args.version}-{runtime}/licenses/python/THIRD_PARTY_NOTICES.txt" in members
    if mode == "onefile":
        assert not dist.exists()
        assert not (tmp_path / "__main__.build").exists()
        assert binary.exists()
    else:
        assert (tmp_path / f"{args.app_name}.dist" / args.app_name).exists()


def test_cleanup_refuses_escape_and_symlink(tmp_path):
    outside = tmp_path / "source"
    outside.mkdir()
    output = tmp_path / "dist"
    output.mkdir()
    with pytest.raises(ValueError):
        build.remove_output(outside, output)
    assert outside.exists()


def test_dry_run_has_no_side_effects(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(build.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(build.platform, "system", lambda: "Linux")
    monkeypatch.setattr(build, "prepare", lambda args: pytest.fail("dry run prepared files"))
    monkeypatch.setattr(build, "run", lambda *args, **kwargs: pytest.fail("dry run ran a command"))
    output = tmp_path / "absent"
    assert build.main(["--dry-run", "--output-dir", str(output)]) == 0
    assert not output.exists()
    assert "Linux archive=True" in capsys.readouterr().out


def test_skipped_frontend_requires_existing_dist(monkeypatch, tmp_path):
    args = options(monkeypatch, "Linux", "--skip-webui")
    monkeypatch.setattr(build, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="webui/dist/index.html"):
        build.prepare(args)


def test_macos_signing_and_notarization_commands(monkeypatch, tmp_path):
    args = options(monkeypatch, "Darwin", "--sign-identity", "Developer ID Application: Example", "--notarize", "--notary-profile", "test-profile")
    calls = []
    monkeypatch.setattr(build, "run", lambda command, **kwargs: calls.append([str(x) for x in command]))
    app = tmp_path / "ArgusAI.app"
    build.sign(app, args)
    build.notarize(app, args)
    assert "--options" in calls[0] and "runtime" in calls[0]
    assert "--entitlements" in calls[0]
    assert any(command[:3] == ["xcrun", "notarytool", "submit"] and "test-profile" in command for command in calls)
    assert calls[-1] == ["xcrun", "stapler", "staple", str(app)]


def test_macos_app_copies_vendor_before_signing(monkeypatch, tmp_path):
    args = options(monkeypatch, "Darwin", "--dmg")
    args.output = tmp_path / "output"
    source = tmp_path / "source"
    monkeypatch.setattr(build, "ROOT", source)
    app = args.output / "__main__.app"
    resources = app / "Contents/Resources"
    populate_payload(resources, args)
    binary = app / "Contents/MacOS/__main__"
    binary.parent.mkdir(parents=True)
    binary.write_text("Mach-O fixture")
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": "__main__"}))
    for relative in ("runners/task.js", "sqlmap/sqlmap.py"):
        target = source / "argusai/vendor" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("retained vendor fixture")
    calls = []
    def sign_app(path, args):
        assert (path / "Contents/Resources/argusai/vendor/runners/task.js").is_file()
        assert (path / "Contents/Resources/argusai/vendor/sqlmap/sqlmap.py").read_text() == "retained vendor fixture"
        assert (path / "Contents/Resources/licenses/LICENSE").is_file()
        calls.append("signed")
    monkeypatch.setattr(build, "sign", sign_app)
    monkeypatch.setattr(build, "run", lambda command, **kwargs: calls.append([str(x) for x in command]))
    result = build.finish(args)
    assert result.name == f"ArgusAI-{args.version}-macos-x64.dmg"
    assert calls[1] == "signed"
    assert calls[-1][0:2] == ["bash", "scripts/macos/make_dmg.sh"]


def test_compile_failure_does_not_package_stale_output(monkeypatch, tmp_path):
    args = options(monkeypatch)
    args.output = tmp_path
    stale = tmp_path / "__main__.dist"
    stale.mkdir()
    (stale / "old").write_text("old output")
    monkeypatch.setattr(build, "parse_args", lambda argv: args)
    monkeypatch.setattr(build, "prepare", lambda args: None)
    def fail_compile(*a, **kw):
        assert not stale.exists()
        raise build.subprocess.CalledProcessError(1, "nuitka")
    monkeypatch.setattr(build, "run", fail_compile)
    monkeypatch.setattr(build, "finish", lambda args: pytest.fail("packaged a failed build"))
    assert build.main([]) == 1


@pytest.mark.parametrize("mode", ["folder", "onefile"])
@pytest.mark.parametrize("machine,arch", [("AMD64", "x86_64"), ("ARM64", "arm64")])
def test_nsis_defines_and_notice_encoding(monkeypatch, tmp_path, mode, machine, arch):
    args = options(monkeypatch, "Windows", "--mode", mode, machine=machine)
    args.output = tmp_path
    args.nsis_path = Path("makensis.exe")
    calls = []
    def fake_run(command, **kwargs):
        command = [str(value) for value in command]
        calls.append(command)
        if "/INPUTCHARSET" in command:
            for flag in command:
                if flag.startswith("/DNOTICE_"):
                    notice = Path(flag.split("=", 1)[1]).read_bytes()
                    assert notice.startswith(b"\xef\xbb\xbf")
                    assert b"GNU GENERAL PUBLIC LICENSE" in notice
                    assert b"Version 3, 29 June 2007" in notice
                if flag.startswith("/DOUT_FILE="):
                    Path(flag.split("=", 1)[1]).write_text("installer fixture")
    monkeypatch.setattr(build, "run", fake_run)
    installer = build.nsis(tmp_path / "payload", args)
    assert installer.exists()
    assert f"/DAPP_ARCH={arch}" in calls[-1]
    assert ("win-arm64" in installer.name) == (arch == "arm64")
    assert f"/DINSTALL_MODE={mode}" in calls[-1]
    assert any(flag.startswith("/DPAYLOAD_FILE=" if mode == "onefile" else "/DDIST_DIR=") for flag in calls[-1])
    assert calls[-1][1:3] == ["/INPUTCHARSET", "UTF8"]


def test_runtime_assets_and_version_stay_consistent():
    for name in build.REMOVED_RUNTIMES:
        assert not (REPO / "argusai/vendor" / name).exists()
    package = json.loads((REPO / "argusai/webui/package.json").read_text())
    assert package["version"] == build.version()
    for path in ("scripts/macos/argusai.entitlements", "scripts/macos/make_dmg.sh", "scripts/windows/argusai.nsi", "scripts/windows/launch-gui.vbs"):
        assert (REPO / path).is_file()
