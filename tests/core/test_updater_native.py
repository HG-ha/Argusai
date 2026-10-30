"""Real, isolated installer-helper smoke tests (also executable over SSH).

Run: python test_updater_native.py /absolute/path/to/updater.py
Only temporary fixture applications are created or replaced.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest


MODULE = Path(sys.argv[1]).resolve() if __name__ == "__main__" and len(sys.argv) > 1 else Path(__file__).resolve().parents[2] / "argusai/core/updater.py"
PREPARE = """import sys,json,pathlib,importlib.util,types
try: import httpx
except ImportError: sys.modules['httpx']=types.ModuleType('httpx')
spec=importlib.util.spec_from_file_location('native_updater',sys.argv[1]);u=importlib.util.module_from_spec(spec);spec.loader.exec_module(u)
c=json.loads(sys.argv[2]);m=u.UpdateManager(pathlib.Path(c['cache']),'1.0.0',platform_key=c['key'],executable=pathlib.Path(c['exe']),packaged=True)
m._manifest=u.validate_manifest(c['manifest']);m._package=pathlib.Path(c['package']);m._state['status']='downloaded'
r=m.prepare_install();print(json.dumps(r));sys.exit(0 if r['ok'] else 2)
"""


class NativeUpdaterSmoke(unittest.TestCase):
    def prepare(self, root, key, executable, package):
        body = package.read_bytes()
        config = {
            "cache": str(root / "cache"), "key": key, "exe": str(executable), "package": str(package),
            "manifest": {"schema_version": 1, "version": "1.0.1", "notes": "Native fixture",
                         "release_url": "https://github.com/HG-ha/Argusai/releases/tag/v1.0.1",
                         "assets": {key: {"url": "https://github.com/HG-ha/Argusai/releases/download/v1.0.1/" + package.name,
                                          "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}}},
        }
        process = subprocess.run([sys.executable, "-c", PREPARE, str(MODULE), json.dumps(config)],
                                 capture_output=True, text=True, timeout=180)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        return json.loads(process.stdout)

    @unittest.skipUnless(platform.system() == "Windows", "Requires native Windows tools")
    def test_windows_fixture_installer_swap_and_failed_install_rollback(self):
        system = Path(os.environ.get("SystemRoot", "C:/Windows"))
        csc = system / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        nsis = shutil.which("makensis")
        if not nsis:
            for variable in ("ProgramFiles(x86)", "ProgramFiles"):
                candidate = Path(os.environ.get(variable, "C:/Program Files")) / "NSIS/makensis.exe"
                if candidate.is_file():
                    nsis = str(candidate)
                    break
        if not csc.is_file() or not nsis:
            self.skipTest("Requires Windows C# compiler and NSIS for isolated executable fixtures")
        powershell = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
        for failure in ("none", "installer_error", "wrong_version"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(prefix="argusai-updater-windows-") as directory:
                root = Path(directory).resolve()
                application = root / "installed"
                (application / "licenses").mkdir(parents=True)
                (application / "licenses/LICENSE").write_text("GPL")
                (application / "Uninstall.exe").write_bytes(b"fixture-uninstaller")
                (application / "launch-gui.vbs").write_text("' inert fixture; never launch a real app")
                executable = application / "ArgusAI.exe"
                executable.write_bytes(b"previous fixture executable")
                (application / "old.txt").write_text("old application")
                version = "1.0.2" if failure == "wrong_version" else "1.0.1"
                source = root / "fixture.cs"
                source.write_text('using System; class Fixture { static int Main(string[] a) { Console.WriteLine("{\\\"version\\\":\\\"' + version + '\\\"}"); return 0; } }')
                payload = root / "fixture.exe"
                subprocess.run([str(csc), "/nologo", "/target:exe", "/out:" + str(payload), str(source)],
                               check=True, capture_output=True, timeout=30)
                package = root / "ArgusAI-1.0.1-win-x64-setup.exe"
                script = root / "fixture.nsi"
                script.write_text('Unicode True\nRequestExecutionLevel user\nSilentInstall silent\nName "ArgusAI isolated updater fixture"\nOutFile "' + str(package) + '"\nSection\nSetOutPath "$INSTDIR"\nFile /oname=ArgusAI.exe "' + str(payload) + '"\n' + ('SetErrorLevel 9\n' if failure == "installer_error" else '') + 'SectionEnd\n')
                subprocess.run([nsis, "/INPUTCHARSET", "UTF8", str(script)], check=True,
                               capture_output=True, timeout=30)
                ready = self.prepare(root, "win-x64", executable, package)
                elevated = next((root / "cache").glob("install-elevated-*.ps1"))
                # Run the privileged phase directly in this writable temporary
                # fixture. No UAC elevation, real installer, registry or shortcut.
                process = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                                          "Bypass", "-File", str(elevated)], capture_output=True, text=True, timeout=45)
                report = json.loads((root / "cache/install-result.json").read_text(encoding="utf-8-sig"))
                if failure == "none":
                    self.assertEqual(process.returncode, 0, process.stdout + process.stderr + json.dumps(report))
                    self.assertTrue(report["ok"])
                    self.assertTrue(Path(report["backup"]).joinpath("old.txt").is_file())
                    metadata = json.loads(subprocess.check_output([str(executable), "version", "--json"], text=True))
                    self.assertEqual(metadata["version"], "1.0.1")
                else:
                    self.assertNotEqual(process.returncode, 0)
                    self.assertFalse(report["ok"])
                    self.assertEqual(executable.read_bytes(), b"previous fixture executable")
                    self.assertEqual((application / "old.txt").read_text(), "old application")

    @unittest.skipUnless(platform.system() in {"Linux", "Darwin"}, "Requires a POSIX host")
    def test_linux_directory_swap_and_failed_swap_rollback(self):
        for fail in (False, True):
            with self.subTest(rollback=fail), tempfile.TemporaryDirectory(prefix="argusai-updater-linux-") as directory:
                root = Path(directory).resolve()
                app = root / "installed"
                (app / "licenses").mkdir(parents=True)
                (app / "licenses/LICENSE").write_text("GPL")
                (app / "licenses/SOURCE_CODE.txt").write_text("fixture")
                executable = app / "ArgusAI"
                executable.write_text("#!/bin/sh\nexit 0\n")
                executable.chmod(0o755)
                (app / "old.txt").write_text("old application")
                marker = root / "launched"
                source = root / "source/ArgusAI-1.0.1-linux-x64"
                source.mkdir(parents=True)
                (source / "ArgusAI").write_text("#!/bin/sh\nif [ \"${1:-}\" = version ]; then printf '%s\\n' '{\"version\":\"1.0.1\"}'; exit 0; fi\nprintf updated > '" + str(marker) + "'\n")
                (source / "ArgusAI").chmod(0o755)
                (source / "new.txt").write_text("new application")
                package = root / "ArgusAI-1.0.1-linux-x64.tar.gz"
                with tarfile.open(package, "w:gz") as archive:
                    archive.add(source, arcname=source.name)
                ready = self.prepare(root, "linux-x64", executable, package)
                if fail:
                    staged = next(root.glob(".argusai-update-*")) / source.name
                    self.assertTrue(staged.resolve().is_relative_to(root))
                    shutil.rmtree(staged)
                process = subprocess.run(ready["command"], capture_output=True, text=True, timeout=15)
                if fail:
                    self.assertNotEqual(process.returncode, 0)
                    self.assertEqual((app / "old.txt").read_text(), "old application")
                    self.assertFalse((app / "new.txt").exists())
                    self.assertFalse(json.loads((root / "cache/install-result.json").read_text())["ok"])
                else:
                    self.assertEqual(process.returncode, 0, process.stderr)
                    self.assertEqual((app / "new.txt").read_text(), "new application")
                    self.assertTrue(next(root.glob("installed.previous-*")).joinpath("old.txt").is_file())
                    for _ in range(50):
                        if marker.exists():
                            break
                        time.sleep(0.1)
                    self.assertEqual(marker.read_text(), "updated")

    @unittest.skipUnless(platform.system() == "Darwin", "Requires native macOS tools")
    def test_macos_dmg_staging_signed_bundle_swap_and_backup(self):
        for binary_name in ("__main__", "ArgusAI"):
            with self.subTest(executable=binary_name), tempfile.TemporaryDirectory(prefix="argusai-updater-macos-") as directory:
                root = Path(directory).resolve()
                marker = root / "launched"

                def bundle(path, version):
                    executable = path / "Contents/MacOS" / binary_name
                    executable.parent.mkdir(parents=True)
                    executable.write_text("#!/bin/sh\nif [ \"${1:-}\" = version ]; then printf '%s\\n' '{\"version\":\"" + version + "\"}'; exit 0; fi\nprintf '" + version + "' > '" + str(marker) + "'\n")
                    executable.chmod(0o755)
                    with (path / "Contents/Info.plist").open("wb") as stream:
                        plistlib.dump({"CFBundleExecutable": binary_name, "CFBundleIdentifier": "com.argusai.update.test.fixture",
                                      "CFBundleName": "ArgusAI", "CFBundleVersion": version,
                                      "CFBundlePackageType": "APPL"}, stream)
                    subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(path)],
                                   check=True, capture_output=True)
                    return executable

                installed = root / "ArgusAI.app"
                executable = bundle(installed, "1.0.0")
                new = root / "image-source/ArgusAI.app"
                bundle(new, "1.0.1")
                package = root / "ArgusAI-1.0.1-macos-arm64.dmg"
                subprocess.run(["/usr/bin/hdiutil", "create", "-volname", "ArgusAI-Updater-Fixture",
                                "-srcfolder", str(new.parent), "-ov", "-format", "UDZO", str(package)],
                               check=True, capture_output=True, timeout=120)
                ready = self.prepare(root, "macos-arm64", executable, package)
                process = subprocess.run(ready["command"], capture_output=True, text=True, timeout=15)
                self.assertEqual(process.returncode, 0, process.stderr)
                with (installed / "Contents/Info.plist").open("rb") as stream:
                    self.assertEqual(plistlib.load(stream)["CFBundleVersion"], "1.0.1")
                backup = next(root.glob("ArgusAI.app.previous-*"))
                with (backup / "Contents/Info.plist").open("rb") as stream:
                    self.assertEqual(plistlib.load(stream)["CFBundleVersion"], "1.0.0")
                subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(installed)],
                               check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]], verbosity=2)
