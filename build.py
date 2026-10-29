#!/usr/bin/env python3
"""Build native ArgusAI releases on Windows, macOS and Linux.

Run with the project interpreter: uv run --group build python build.py --help.
No downloads, compilation or filesystem changes occur with --dry-run.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REMOVED_RUNTIMES = ("python-runtime", "node-runtime", "js-tools", "wabt")
GHIDRA_SCRIPTS = ("decompile_function.py", "disassemble_function.py", "list_functions.py")
WINDOWS_ARM_RUNTIME_CONFIG = "argusai/gui/windows-arm64.runtimeconfig.json"
LEGAL_FILES = ("LICENSE", "THIRD_PARTY_NOTICES.md", "SOURCE_CODE.txt",
               "python/THIRD_PARTY_NOTICES.txt", "python/manifest.json")


def run(args: list[str | Path], *, cwd: Path = ROOT) -> None:
    command = [str(part) for part in args]
    print("+ " + shlex.join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def version() -> str:
    source = (ROOT / "argusai/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', source)
    if not match:
        raise ValueError("Cannot read argusai.__version__")
    return match[1]


def architecture(value: str) -> str:
    aliases = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}
    value = value.lower()
    value = aliases.get(value, value)
    if value not in {"x86_64", "arm64"}:
        raise ValueError(f"Unsupported architecture: {value}")
    return value


def parse_args(argv: list[str] | None = None, *, system: str | None = None) -> argparse.Namespace:
    system = system or platform.system()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples (run on the target OS):
  uv run --group build python build.py --jobs 2
  uv run --group build python build.py --onefile --skip-webui
  uv run --group build python build.py --nsis             # Windows + NSIS 3
  uv run --group build python build.py --dmg              # macOS + Xcode CLT
  uv run --group build python build.py --dmg --sign-identity auto \\
      --notarize --notary-profile argusai-notary

Defaults: macOS app bundle; Windows/Linux standalone folder. Linux also
creates a versioned tar.gz. A C toolchain is required; Linux needs patchelf.
Windows ARM64 requires ARM64 Python and the Visual Studio ARM64 C++ tools;
Nuitka is forced to use MSVC for that target. Build ARM64 on an ARM64 runner.
Frontend dependencies are installed with npm ci unless --skip-webui is set.
Use --output-dir for a dedicated directory; known older build outputs there
are replaced. Running applications are never terminated automatically.
Apple Silicon -> Intel: prepare scripts/setup_macos_x64_venv.sh, then run
arch -x86_64 .venv-x64/bin/python build.py --arch x86_64 --dmg.
No Developer ID certificate is required for default ad-hoc macOS builds.
For notarization, first store credentials with xcrun notarytool store-credentials.
""")
    parser.add_argument("--mode", type=str.lower, choices=("folder", "onefile", "app"))
    parser.add_argument("--onefile", action="store_true", help="Alias for --mode onefile")
    parser.add_argument("--output-dir", default="dist")
    parser.add_argument("--app-name", default="ArgusAI")
    parser.add_argument("--jobs", "-j", type=int, default=os.environ.get("ARGUSAI_BUILD_JOBS", "0"))
    parser.add_argument("--arch", choices=("native", "x86_64", "arm64"), default="native")
    parser.add_argument("--console", action="store_true")
    parser.add_argument("--skip-webui", action="store_true", help="Reuse webui/dist and verify its third-party notices against installed dependencies")
    parser.add_argument("--fast", action="store_true", help="Disable onefile compression for local iteration")
    parser.add_argument("--icon", type=Path)
    parser.add_argument("--nsis", action="store_true", help="Windows: create an installer")
    parser.add_argument("--nsis-path", type=Path)
    parser.add_argument("--dmg", action="store_true", help="macOS: create a drag-to-Applications DMG")
    parser.add_argument("--bundle-id", default="com.argusai.app")
    parser.add_argument("--sign-identity", default=os.environ.get("ARGUSAI_CODESIGN_IDENTITY", "-"))
    parser.add_argument("--notarize", action="store_true")
    parser.add_argument("--notary-profile", default=os.environ.get("ARGUSAI_NOTARY_PROFILE", ""))
    parser.add_argument("--dry-run", action="store_true", help="Print the build plan without modifying files")
    args = parser.parse_args(argv)
    args.system = system
    if system not in {"Windows", "Darwin", "Linux"}:
        parser.error(f"Unsupported system: {system}")
    args.mode = "onefile" if args.onefile else args.mode or ("app" if system == "Darwin" else "folder")
    if args.jobs < 0:
        parser.error("--jobs must be >= 0")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.app_name):
        parser.error("--app-name must be a filename using letters, numbers, dots, underscores or hyphens")
    if not args.output_dir.strip():
        parser.error("--output-dir cannot be empty")
    args.output = (ROOT / args.output_dir).resolve()
    # Cleanup is limited to known children of a dedicated output directory.
    if args.output.parent == args.output or ROOT.is_relative_to(args.output) or (args.output.is_relative_to(ROOT) and args.output.parts[len(ROOT.parts)] in {"argusai", "assets", "scripts", "tests", ".git", ".venv", ".github"}):
        parser.error("--output-dir must be a dedicated build directory")
    if args.nsis and system != "Windows":
        parser.error("--nsis requires Windows")
    if (args.dmg or args.notarize or args.mode == "app") and system != "Darwin":
        parser.error("App bundles, DMG and notarization require macOS")
    if args.dmg and args.mode != "app":
        parser.error("--dmg requires --mode app")
    if args.notarize and (args.sign_identity in {"", "-", "adhoc"} or not args.notary_profile):
        parser.error("--notarize requires --sign-identity and --notary-profile")
    if args.notarize and args.mode != "app":
        parser.error("--notarize requires --mode app (optionally with --dmg)")
    args.host_arch = architecture(platform.machine())
    args.python_arch = args.host_arch
    if system == "Windows":
        # machine() can describe the ARM64 host while Python runs under x64
        # emulation. Wheel/extension ABI follows the interpreter, not that host.
        python_platform = sysconfig.get_platform().lower()
        python_arch = {"win-amd64": "x86_64", "win-arm64": "arm64"}.get(python_platform)
        if python_arch is None:
            parser.error(f"Use a 64-bit Windows Python interpreter (win-amd64 or win-arm64), got {python_platform}")
        args.python_arch = python_arch
    args.target_arch = args.python_arch if args.arch == "native" else args.arch
    if args.target_arch != args.python_arch:
        parser.error("Use a Python interpreter and dependencies for the target architecture; on Apple Silicon run the x86_64 environment under Rosetta")
    label = {"Windows": "win", "Darwin": "macos", "Linux": "linux"}[system]
    args.runtime_arch = f"{label}-{'x64' if args.target_arch == 'x86_64' else 'arm64'}"
    args.version = version()
    return args


def resolve_identity(identity: str) -> str:
    if identity == "adhoc":
        return "-"
    if identity != "auto":
        return identity
    result = subprocess.check_output(["security", "find-identity", "-v", "-p", "codesigning"], text=True)
    identities = re.findall(r'"(Developer ID Application:[^"]+)"', result)
    if len(identities) != 1:
        raise ValueError("--sign-identity auto requires exactly one Developer ID Application certificate")
    return identities[0]


def nuitka_args(args: argparse.Namespace, *, win32_helper: Path | None = None) -> list[str]:
    flags = [
        "--standalone", "--enable-plugin=anti-bloat", "--assume-yes-for-downloads",
        f"--output-dir={args.output}", "--company-name=ArgusAI", "--product-name=ArgusAI",
        f"--file-version={args.version}", f"--product-version={args.version}",
        "--include-package=argusai", "--include-package=websockets",
        "--include-package=lark_oapi.core", "--include-package=lark_oapi.ws",
        "--include-package=dingtalk_stream", "--include-package=segno", "--include-package-data=certifi",
        "--include-data-files=argusai/core/reverse/ghidra_scripts/*.py=argusai/core/reverse/ghidra_scripts/",
        "--no-deployment-flag=excluded-module-usage",
    ]
    for package in ("argusai.vendor.sqlmap", "argusai.vendor.sqlmap.*", "lark_oapi.api", "lark_oapi.api.*"):
        flags.append(f"--nofollow-import-to={package}")
    data = ["argusai/webui/dist", "argusai/report/templates", "argusai/skills"]
    flags.append(f"--include-data-dir=build/release-licenses/{args.runtime_arch}=argusai/legal")
    if args.mode != "app":
        data += ["argusai/vendor/runners", "argusai/vendor/sqlmap"]
        vendor = f"argusai/vendor/innoextract/{args.runtime_arch}"
        if (ROOT / vendor).is_dir():
            data.append(vendor)
            for binary in sorted((ROOT / vendor).iterdir()):
                if binary.is_file() and (binary.suffix == ".exe" or binary.name == "innoextract"):
                    relative = binary.relative_to(ROOT).as_posix()
                    flags.append(f"--include-data-files={relative}={relative}")
    flags += [f"--include-data-dir={path}={path}" for path in data]
    for pattern in ("argusai/webui/node_modules/*", "argusai/webui/src/*", "*/__pycache__/*", "*.pyc"):
        flags.append(f"--noinclude-data-files={pattern}")
    node = "node.exe" if args.system == "Windows" else "node"
    flags.append(f"--noinclude-dlls=playwright/driver/{node}")
    if args.system == "Windows":
        flags += ["--include-package=clr_loader", "--include-package=pythonnet",
                  f"--windows-console-mode={'force' if args.console else 'hide'}"]
        if args.target_arch == "arm64":
            flags += ["--msvc=latest",
                      f"--include-data-files={WINDOWS_ARM_RUNTIME_CONFIG}={WINDOWS_ARM_RUNTIME_CONFIG}"]
        if win32_helper:
            flags.append(f"--include-data-files={win32_helper}=webview/platforms/win32.py")
        flags.append(f"--output-filename={args.app_name}.exe")
    elif args.system == "Linux":
        flags.append(f"--output-filename={args.app_name}")
    else:
        flags += [f"--macos-target-arch={args.target_arch}", f"--macos-app-mode={'console' if args.console else 'gui'}"]
        if args.mode == "app":
            flags += ["--macos-create-app-bundle", f"--macos-app-name={args.app_name}",
                      f"--macos-app-version={args.version}", f"--macos-signed-app-name={args.bundle_id}"]
        if args.sign_identity not in {"", "-", "adhoc"}:
            flags += [f"--macos-sign-identity={args.sign_identity}", "--macos-sign-notarization"]
    if args.mode == "onefile":
        flags += ["--onefile", "--onefile-tempdir-spec={CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION}"]
        if args.fast:
            flags.append("--onefile-no-compression")
    icon = args.icon or ROOT / "assets/brand" / {
        "Windows": "argusai-icon-Transparency.ico", "Darwin": "argusai-icon.icns", "Linux": "argusai-icon-Transparency.png",
    }[args.system]
    if not icon.is_absolute():
        icon = ROOT / icon
    if icon.is_file():
        option = {"Windows": "windows-icon-from-ico", "Darwin": "macos-app-icon", "Linux": "linux-icon"}[args.system]
        flags.append(f"--{option}={icon}")
    elif args.icon:
        raise ValueError(f"Icon does not exist: {icon}")
    if args.jobs:
        flags.append(f"--jobs={args.jobs}")
    return flags


def remove_output(path: Path, output: Path) -> None:
    if path.is_symlink() or path.resolve().parent != output.resolve():
        raise ValueError(f"Refusing to remove output outside build directory: {path}")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def prepare_legal(args: argparse.Namespace) -> None:
    """Stage the project's terms and available original dependency notices."""
    from scripts.python_notices import generate_notices
    parent = ROOT / "build/release-licenses"
    if not parent.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("License staging directory escapes the source workspace")
    parent.mkdir(parents=True, exist_ok=True)
    output = parent / args.runtime_arch
    with tempfile.TemporaryDirectory(prefix="notices-", dir=parent) as temporary:
        staging = Path(temporary)
        generate_notices(staging / "python")
        for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
            shutil.copyfile(ROOT / name, staging / name)
        commit = "not recorded"
        try:
            commit = subprocess.check_output(
                ["git", "-c", f"safe.directory={ROOT.as_posix()}", "rev-parse", "HEAD"],
                cwd=ROOT, text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            pass
        (staging / "SOURCE_CODE.txt").write_text(
            f"ArgusAI {args.version} ({args.runtime_arch})\nLicense: GPL-3.0-only\n"
            f"Build checkout commit: {commit}\n"
            "Local uncommitted modifications are not identified by that commit.\n"
            "Project: https://github.com/HG-ha/Argusai\n"
            "Releases: https://github.com/HG-ha/Argusai/releases\n"
            f"Published source archive: ArgusAI-{args.version}-source.tar.gz\n"
            "The release workflow archives the same validated commit as the binaries.\n"
            "For modified/local builds, distribute the exact source and build scripts used.\n"
            "Preserve the separate licenses and source obligations of covered dependencies.\n"
            "See THIRD_PARTY_NOTICES.md and python/THIRD_PARTY_NOTICES.txt.\n",
            encoding="utf-8", newline="\n",
        )
        remove_output(output, parent)
        shutil.copytree(staging, output)


def copy_legal(destination: Path, *roots: Path) -> None:
    for root in roots:
        source = root / "argusai/legal"
        if all((source / name).is_file() for name in LEGAL_FILES):
            if source.resolve().is_relative_to(destination.resolve()) or destination.resolve().is_relative_to(source.resolve()):
                raise ValueError("License destination overlaps its embedded source")
            remove_output(destination, destination.parent)
            shutil.copytree(source, destination)
            return
    raise ValueError("Missing complete packaged license directory")


def prepare(args: argparse.Namespace) -> Path | None:
    if args.system == "Windows" and args.target_arch == "arm64":
        runtime_config = ROOT / WINDOWS_ARM_RUNTIME_CONFIG
        if not runtime_config.is_file():
            raise ValueError(f"Missing Windows ARM64 .NET runtime configuration: {runtime_config}")
    if args.system == "Darwin":
        tools = ["codesign", "file"]
        if args.dmg:
            tools += ["bash", "hdiutil"]
        if args.notarize:
            tools += ["xcrun", "ditto"]
        for tool in tools:
            if not shutil.which(tool):
                raise ValueError(f"Missing macOS build tool: {tool}")
    webui = ROOT / "argusai/webui"
    if not args.skip_webui:
        npm = shutil.which("npm.cmd" if args.system == "Windows" else "npm")
        if not npm:
            raise ValueError("Node.js/npm is required to build the frontend")
        run([npm, "ci"], cwd=webui)
        run([npm, "run", "build"], cwd=webui)
    if not (webui / "dist/index.html").is_file():
        raise ValueError("Missing webui/dist/index.html; build without --skip-webui")
    from scripts.frontend_notices import check_notices, generate_notices
    if args.skip_webui:
        check_notices(webui)
    else:
        generate_notices(webui)
    for package in ("nuitka", "zstandard"):
        if importlib.util.find_spec(package) is None:
            raise ValueError("Install build dependencies: uv sync --group build; then uv run --group build python build.py")
    if args.system == "Linux" and not shutil.which("patchelf"):
        raise ValueError("Install patchelf using your system package manager")
    prepare_legal(args)
    vendor = ROOT / "argusai/vendor/innoextract" / args.runtime_arch
    executable = vendor / ("innoextract.exe" if args.system == "Windows" else "innoextract")
    if not executable.exists() and args.runtime_arch in {"win-x64", "linux-x64"}:
        run([sys.executable, "scripts/vendor_runtime.py"])
        if not executable.is_file():
            raise ValueError(f"Vendor installation did not produce {executable}")
    elif not executable.exists() and args.target_arch == "arm64":
        print(f"innoextract is not bundled for {args.runtime_arch}: no official portable ARM64 archive. "
              "Installer unpacking requires a separately configured compatible tool.", flush=True)
    helper = None
    if args.system == "Windows":
        import webview
        helper = Path(webview.__file__).resolve().parent / "platforms/win32.py"
        if not helper.is_file():
            raise ValueError(f"Missing pywebview helper: {helper}")
    args.output.mkdir(parents=True, exist_ok=True)
    return helper


def verify_payload(path: Path, args: argparse.Namespace, *other_roots: Path) -> None:
    roots = (path, *other_roots)
    required = ["argusai/webui/dist/index.html", "argusai/webui/dist/THIRD_PARTY_NOTICES.txt",
                "argusai/vendor/runners", "argusai/vendor/sqlmap/sqlmap.py"]
    required += [f"argusai/core/reverse/ghidra_scripts/{name}" for name in GHIDRA_SCRIPTS]
    required += [f"argusai/legal/{name}" for name in LEGAL_FILES]
    if args.system == "Windows" and args.target_arch == "arm64":
        required.append(WINDOWS_ARM_RUNTIME_CONFIG)
    vendor = ROOT / "argusai/vendor/innoextract" / args.runtime_arch
    if vendor.is_dir():
        required.append(f"argusai/vendor/innoextract/{args.runtime_arch}/{'innoextract.exe' if args.system == 'Windows' else 'innoextract'}")
    missing = [name for name in required if not any((root / name).exists() for root in roots)]
    if missing:
        raise ValueError(f"Missing packaged resources: {missing}")
    forbidden = [f"argusai/vendor/{name}" for name in REMOVED_RUNTIMES]
    forbidden += ["argusai/webui/src", "argusai/webui/node_modules", "playwright/driver/node", "playwright/driver/node.exe"]
    if any((root / name).exists() for root in roots for name in forbidden) or any(any((root / "argusai").rglob("*.pyc")) for root in roots):
        raise ValueError("Build contains excluded runtimes, frontend sources or bytecode")


def sign(path: Path, args: argparse.Namespace) -> None:
    command = ["codesign", "--force", "--sign", args.sign_identity]
    if path.suffix != ".dmg":
        command += ["--deep", "-i", args.bundle_id]
    if args.sign_identity != "-":
        command += ["--timestamp"]
        if path.suffix != ".dmg":
            command += ["--options", "runtime", "--entitlements", str(ROOT / "scripts/macos/argusai.entitlements")]
    run([*command, path])
    run(["codesign", "--verify", "--deep", "--strict", path])


def notarize(path: Path, args: argparse.Namespace) -> None:
    with tempfile.TemporaryDirectory(prefix="argusai-notary-") as temporary:
        archive = path
        if path.suffix != ".dmg":
            archive = Path(temporary) / "app.zip"
            run(["ditto", "-c", "-k", "--keepParent", path, archive])
        run(["xcrun", "notarytool", "submit", archive, "--wait", "--keychain-profile", args.notary_profile])
    run(["xcrun", "stapler", "staple", path])


def nsis(payload: Path, args: argparse.Namespace) -> Path:
    compiler = str(args.nsis_path) if args.nsis_path else shutil.which("makensis")
    if not compiler:
        for variable in ("ProgramFiles(x86)", "ProgramFiles"):
            candidate = Path(os.environ.get(variable, "C:/Program Files")) / "NSIS/makensis.exe"
            if candidate.is_file():
                compiler = str(candidate)
                break
    if not compiler:
        raise ValueError("Install NSIS 3 or pass --nsis-path")
    run([sys.executable, "scripts/windows/make_nsis_art.py"])
    installer = args.output / f"{args.app_name}-{args.version}-{args.runtime_arch}-setup.exe"
    defines = {
        "APP_NAME": args.app_name, "APP_VERSION": args.version,
        "APP_VERSION_QUAD": ".".join((re.findall(r"\d+", args.version) + ["0"] * 4)[:4]),
        "PUBLISHER": "ArgusAI", "OUT_FILE": str(installer),
        "APP_ARCH": args.target_arch,
        "INSTALL_MODE": "onefile" if args.mode == "onefile" else "folder",
        "PAYLOAD_FILE" if args.mode == "onefile" else "DIST_DIR": str(payload),
        "ICON_FILE": str(ROOT / "assets/brand/argusai-icon-Transparency.ico"),
        "LICENSES_DIR": str(payload.parent / "licenses"),
        "LAUNCH_VBS": str(ROOT / "scripts/windows/launch-gui.vbs"),
        "WELCOME_BMP": str(ROOT / "scripts/windows/nsis-ui/welcome.bmp"),
        "HEADER_BMP": str(ROOT / "scripts/windows/nsis-ui/header.bmp"),
    }
    with tempfile.TemporaryDirectory(prefix="argusai-nsis-") as temporary:
        for locale in ("zh", "en"):
            notice = Path(temporary) / f"notice.{locale}.txt"
            terms = (ROOT / "LICENSE").read_text(encoding="utf-8")
            guidance = (ROOT / f"scripts/packaging/notice.{locale}.txt").read_text(encoding="utf-8-sig")
            notice.write_text(terms + "\n\n--- Product information / 产品说明 ---\n\n" + guidance,
                              encoding="utf-8-sig")
            defines[f"NOTICE_{locale.upper()}"] = str(notice)
        run([compiler, "/INPUTCHARSET", "UTF8", *(f"/D{key}={value}" for key, value in defines.items()), ROOT / "scripts/windows/argusai.nsi"])
    if not installer.is_file():
        raise ValueError(f"NSIS did not produce {installer}")
    return installer


def finish(args: argparse.Namespace) -> Path:
    output = args.output
    stem = args.app_name
    if args.mode == "app":
        original = output / "__main__.app"
        payload = output / f"{stem}.app"
        original.rename(payload)
        resources = payload / "Contents/Resources"
        for name in ("runners", "sqlmap", f"innoextract/{args.runtime_arch}"):
            source = ROOT / "argusai/vendor" / name
            if source.exists():
                shutil.copytree(source, resources / "argusai/vendor" / name, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
        # Nuitka may place regular resources beside the executable or in Resources.
        verify_payload(resources, args, payload / "Contents/MacOS")
        copy_legal(resources / "licenses", resources, payload / "Contents/MacOS")
        with (payload / "Contents/Info.plist").open("rb") as stream:
            executable = plistlib.load(stream)["CFBundleExecutable"]
        run(["file", payload / "Contents/MacOS" / executable])
        sign(payload, args)
        if args.notarize:
            notarize(payload, args)
        if args.dmg:
            run([sys.executable, "scripts/macos/make_dmg_art.py"])
            image = output / f"{stem}-{args.version}-{args.runtime_arch}.dmg"
            run(["bash", "scripts/macos/make_dmg.sh", payload, image, stem])
            if args.sign_identity != "-":
                sign(image, args)
            if args.notarize:
                notarize(image, args)
            return image
        return payload
    dist = output / "__main__.dist"
    verify_payload(dist, args)
    copy_legal(dist / "licenses" if args.mode == "folder" else output / "licenses", dist)
    extension = ".exe" if args.system == "Windows" else ""
    executable = f"{stem}{extension}"
    if args.mode == "folder":
        old_executable = dist / ("__main__" + extension)
        if old_executable.exists() and old_executable.name != executable:
            old_executable.rename(dist / executable)
        payload = output / f"{stem}.dist"
        dist.rename(payload)
        if not (payload / executable).is_file():
            raise ValueError(f"Missing launcher: {payload / executable}")
    else:
        payload = output / executable
        if not payload.exists():
            for name in ("__main__.bin", "__main__", "__main__.exe"):
                if (output / name).is_file():
                    (output / name).rename(payload)
                    break
        if not payload.is_file():
            raise ValueError(f"Missing onefile binary: {payload}")
    if args.system == "Darwin":
        sign(payload if args.mode == "onefile" else payload / executable, args)
    result = nsis(payload, args) if args.nsis else payload
    if args.system == "Linux":
        archive_stem = f"{stem}-{args.version}-{args.runtime_arch}"
        result = output / f"{archive_stem}.tar.gz"
        def release_metadata(info: tarfile.TarInfo) -> tarfile.TarInfo:
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if info.name == f"{archive_stem}/{stem}":
                info.mode |= 0o111
            return info
        with tarfile.open(result, "w:gz") as archive:
            archive.add(payload, arcname=f"{archive_stem}/{stem}" if payload.is_file() else archive_stem, filter=release_metadata)
            if payload.is_file():
                archive.add(output / "licenses", arcname=f"{archive_stem}/licenses", filter=release_metadata)
    if args.mode == "onefile":
        for base in ("__main__", stem):
            for suffix in (".dist", ".build", ".onefile-build"):
                remove_output(output / (base + suffix), output)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.dry_run:
            print(f"Plan: {args.system} {args.runtime_arch}, {args.mode}, version {args.version}")
            print("Frontend: " + ("reuse existing dist + verify third-party notices" if args.skip_webui else "npm ci + npm run build + collect third-party notices"))
            print(shlex.join([sys.executable, "-m", "nuitka", *nuitka_args(args), "argusai/__main__.py"]))
            print(f"Postprocessing: NSIS={args.nsis}, DMG={args.dmg}, notarize={args.notarize}, Linux archive={args.system == 'Linux'}")
            return 0
        if sys.version_info < (3, 12):
            raise ValueError("Build with Python 3.12 or newer")
        if args.system == "Darwin":
            args.sign_identity = resolve_identity(args.sign_identity)
        helper = prepare(args)
        # Clear only known products, so a failed compiler cannot reuse stale output.
        names = {f"{base}{suffix}" for base in ("__main__", args.app_name)
                 for suffix in (".dist", ".build", ".onefile-build", ".app", ".exe", ".bin", "")}
        for name in names:
            remove_output(args.output / name, args.output)
        run([sys.executable, "-m", "nuitka", *nuitka_args(args, win32_helper=helper), "argusai/__main__.py"])
        print(f"Built: {finish(args)}")
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
