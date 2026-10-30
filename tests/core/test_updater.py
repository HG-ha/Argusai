import hashlib
import io
import json
import plistlib
import struct
import tarfile
import threading
import time
from pathlib import Path

import httpx
import pytest

from argusai.core.updater import (
    MANIFEST_URL, MAX_MANIFEST_BYTES, UpdateManager, _safe_extract_linux,
    runtime_platform, validate_manifest,
    _macos_launcher,
    _update_parent_pids,
)


def manifest(body=b"installer", version="0.1.4"):
    return {
        "schema_version": 1, "version": version, "published_at": "2026-10-30T14:19:49Z",
        "release_url": f"https://github.com/HG-ha/Argusai/releases/tag/v{version}",
        "notes": "# Update\n\n- New feature",
        "assets": {"win-x64": {
            "url": f"https://github.com/HG-ha/Argusai/releases/download/v{version}/ArgusAI-{version}-win-x64-setup.exe",
            "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
        }},
    }


def manager(tmp_path, payload=b"installer", handler=None, **kwargs):
    record = manifest(payload)
    calls = []
    def route(request):
        calls.append(str(request.url))
        if str(request.url) == MANIFEST_URL:
            return httpx.Response(200, json=record)
        return handler(request) if handler else httpx.Response(200, content=payload)
    client = httpx.Client(transport=httpx.MockTransport(route))
    executable = tmp_path / "installed/ArgusAI.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"old executable")
    (executable.parent / "licenses").mkdir()
    (executable.parent / "licenses/LICENSE").write_text("GPL")
    (executable.parent / "Uninstall.exe").write_bytes(b"uninstaller")
    (executable.parent / "launch-gui.vbs").write_text("' fixture")
    mgr = UpdateManager(tmp_path / "updates", "0.1.3", client=client,
                        platform_key="win-x64", executable=executable, packaged=True, **kwargs)
    return mgr, record, calls


def wait_download(mgr):
    mgr._worker.join(timeout=3)
    assert not mgr._worker.is_alive()
    return mgr.status()


@pytest.mark.parametrize("name", ["__main__", "ArgusAI"])
def test_macos_uses_plist_executable_instead_of_bundle_name(tmp_path, name):
    bundle = tmp_path / "ArgusAI.app"
    binary = bundle / "Contents/MacOS" / name
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"application")
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": name}))
    assert _macos_launcher(bundle) == binary


@pytest.mark.parametrize("name", ["", "..", "../outside", "nested/program", "nested\\program"])
def test_macos_rejects_executable_names_outside_bundle(tmp_path, name):
    bundle = tmp_path / "ArgusAI.app"
    (bundle / "Contents").mkdir(parents=True)
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": name}))
    with pytest.raises(ValueError):
        _macos_launcher(bundle)


def test_packaged_onefile_uses_installed_bootstrap_not_cache(tmp_path, monkeypatch):
    from argusai.core.infra import runtime_exec

    outer = tmp_path / "installed/ArgusAI"
    outer.parent.mkdir()
    outer.write_bytes(b"onefile bootstrap")
    (outer.parent / "licenses").mkdir()
    for name in ("LICENSE", "SOURCE_CODE.txt"):
        (outer.parent / "licenses" / name).write_text("fixture")
    cached = tmp_path / "cache/python.bin"
    cached.parent.mkdir()
    cached.write_bytes(b"cached child")
    monkeypatch.setattr(runtime_exec.sys, "argv", [str(outer), "--gui"])
    monkeypatch.setattr(runtime_exec.sys, "executable", str(cached))
    mgr = UpdateManager(tmp_path / "updates", "0.1.3", platform_key="linux-x64", packaged=True)
    assert mgr.executable == outer.resolve()
    assert mgr.status()["install_mode"] == "automatic"


@pytest.mark.parametrize("parent", ["", "0", "-1", "1; echo injected", "12345678901234567890"])
def test_update_parent_ids_reject_invalid_onefile_environment(monkeypatch, parent):
    monkeypatch.setenv("NUITKA_ONEFILE_PARENT", parent)
    monkeypatch.setattr("argusai.core.updater.os.getpid", lambda: 123)
    assert _update_parent_pids() == (123,)


def test_update_waits_for_onefile_bootstrap_as_well_as_child(monkeypatch):
    monkeypatch.setattr("argusai.core.updater.os.getpid", lambda: 123)
    monkeypatch.setenv("NUITKA_ONEFILE_PARENT", "456")
    assert _update_parent_pids() == (123, 456)
    monkeypatch.setenv("NUITKA_ONEFILE_PARENT", "123")
    assert _update_parent_pids() == (123,)


def test_check_download_verify_prepare_and_restore(tmp_path):
    mgr, _, calls = manager(tmp_path)
    assert mgr.check()["status"] == "available"
    assert mgr.check()["status"] == "available"
    assert len(calls) == 1  # startup throttling
    mgr.start_download()
    assert wait_download(mgr)["status"] == "downloaded"
    assert not list(mgr.cache_dir.glob("*.part"))
    restored = UpdateManager(mgr.cache_dir, "0.1.3", platform_key="win-x64", packaged=True,
                             executable=mgr.executable)
    assert restored.status()["status"] == "downloaded"
    result = mgr.prepare_install()
    assert result["ok"]
    helper = Path(result["helper_path"]).read_text(encoding="utf-8-sig")
    elevated = next(mgr.cache_dir.glob("install-elevated-*.ps1")).read_text(encoding="utf-8-sig")
    assert "WaitForExit($remaining)" in helper
    assert "AddMilliseconds(120000)" in helper
    assert "-Verb RunAs" in helper
    assert "launch-gui.vbs" in helper
    assert "SHA256]::Create()" in elevated
    assert "Move-Item -LiteralPath $backup -Destination $target" in elevated
    assert "taskkill" not in helper + elevated
    assert mgr.installation_aborted()["status"] == "downloaded"


@pytest.mark.parametrize("field,value", [
    ("url", "https://github.com/other/Argusai/releases/download/v0.1.4/ArgusAI-0.1.4-win-x64-setup.exe"),
    ("url", "http://github.com/HG-ha/Argusai/releases/download/v0.1.4/ArgusAI-0.1.4-win-x64-setup.exe"),
    ("url", "https://github.com/HG-ha/Argusai/releases/download/v0.1.4/ArgusAI-0.1.4-win-x64-setup.exe?x=1"),
    ("sha256", "bad"), ("size", True), ("size", -1),
])
def test_manifest_rejects_untrusted_assets(field, value):
    record = manifest()
    record["assets"]["win-x64"][field] = value
    with pytest.raises(ValueError):
        validate_manifest(record)


@pytest.mark.parametrize("version", ["0.1.3", "0.1.2"])
def test_does_not_offer_same_or_older_version(tmp_path, version):
    mgr, record, _ = manager(tmp_path)
    record.clear()
    record.update(manifest(version=version))
    assert mgr.check()["status"] == "up_to_date"
    assert mgr.start_download()["status"] == "up_to_date"


def test_corrupt_download_never_ready_and_leaves_no_partial(tmp_path):
    mgr, _, _ = manager(tmp_path, handler=lambda r: httpx.Response(200, content=b"wrong hash"))
    mgr.check()
    mgr.start_download()
    assert wait_download(mgr)["status"] == "error"
    assert not list(mgr.cache_dir.glob("*.part"))
    assert not list(mgr.cache_dir.glob("*.exe"))


def test_changed_package_cannot_install_and_keeps_ready_for_retry(tmp_path):
    mgr, _, _ = manager(tmp_path)
    mgr.check()
    mgr.start_download()
    wait_download(mgr)
    mgr._package.write_bytes(b"corrupted")
    result = mgr.prepare_install()
    assert not result["ok"]
    assert mgr.status()["status"] == "downloaded"
    assert not list(mgr.cache_dir.glob("*.ps1"))


def test_redirect_rejects_arbitrary_hosts(tmp_path):
    mgr, _, calls = manager(tmp_path, handler=lambda r: httpx.Response(302, headers={"location": "https://evil.invalid/package"}))
    mgr.check()
    mgr.start_download()
    assert wait_download(mgr)["status"] == "error"
    assert not any("evil.invalid" in url for url in calls)


def test_github_release_cdn_redirect_supported(tmp_path):
    def response(request):
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"location": "https://release-assets.githubusercontent.com/signed?token=public"})
        return httpx.Response(200, content=b"installer")
    mgr, _, _ = manager(tmp_path, handler=response)
    mgr.check()
    mgr.start_download()
    assert wait_download(mgr)["status"] == "downloaded"


def test_source_install_never_downloads_or_overwrites(tmp_path):
    mgr, _, calls = manager(tmp_path)
    mgr.packaged = False
    mgr.check()
    assert mgr.start_download()["status"] == "available"
    assert len(calls) == 1
    assert not mgr.prepare_install()["ok"]


def test_data_inside_install_blocks_automatic_replacement(tmp_path):
    mgr, _, _ = manager(tmp_path, protected_paths=[tmp_path / "installed/data"])
    mgr.check()
    mgr.start_download()
    wait_download(mgr)
    assert not mgr.prepare_install()["ok"]
    assert mgr.status()["status"] == "downloaded"
    assert mgr.status()["install_mode"] == "manual"


def test_manifest_size_bounded(tmp_path):
    mgr, _, _ = manager(tmp_path)
    mgr._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b" " * (MAX_MANIFEST_BYTES + 1))))
    assert mgr.check()["status"] == "error"
    assert "too large" in mgr.status()["error"]


def test_first_startup_check_is_not_throttled_by_short_system_uptime(tmp_path, monkeypatch):
    mgr, _, calls = manager(tmp_path)
    monkeypatch.setattr("argusai.core.updater.time.monotonic", lambda: 100.0)
    assert mgr.check()["status"] == "available"
    assert len(calls) == 1


def test_windows_architecture_matches_native_installer_architecture(tmp_path):
    path = tmp_path / "ArgusAI.exe"
    pe = bytearray(512)
    pe[:2] = b"MZ"
    struct.pack_into("<I", pe, 0x3C, 128)
    pe[128:132] = b"PE\0\0"
    struct.pack_into("<H", pe, 132, 0x8664)
    path.write_bytes(pe)
    assert runtime_platform("Windows", "arm64", path) == "win-arm64"
    struct.pack_into("<H", pe, 132, 0xAA64)
    path.write_bytes(pe)
    assert runtime_platform("Windows", "AMD64", path) == "win-x64"


@pytest.mark.parametrize("name,link", [("../escape", None), ("/absolute", None),
                                      ("ArgusAI-0.1.4-linux-x64/link", "../../escape")])
def test_linux_archive_rejects_traversal_and_escaping_links(tmp_path, name, link):
    archive = tmp_path / "ArgusAI-0.1.4-linux-x64.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, link
            tar.addfile(info)
        else:
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises((ValueError, tarfile.FilterError)):
        _safe_extract_linux(archive, tmp_path / "stage", "0.1.4")
    assert not (tmp_path / "escape").exists()


def test_portable_executable_parent_is_never_replaced(tmp_path):
    mgr, _, _ = manager(tmp_path)
    (mgr.executable.parent / "Uninstall.exe").unlink()
    (mgr.executable.parent / "personal.txt").write_text("do not move")
    mgr.check()
    mgr.start_download()
    wait_download(mgr)
    assert not mgr.prepare_install()["ok"]
    assert mgr.status()["install_mode"] == "manual"
    assert (mgr.executable.parent / "personal.txt").read_text() == "do not move"


@pytest.mark.parametrize("report,expect_error", [
    ({"ok": False, "version": "0.1.4", "current_version": "0.1.3", "error": "Installer failed; previous version restored"}, True),
    ({"ok": False, "version": "0.1.2", "current_version": "0.1.3", "error": "Old report"}, False),
    ({"ok": False, "version": "0.1.4", "current_version": "0.1.2", "error": "Old report"}, False),
    ({"ok": True, "version": "0.1.4", "current_version": "0.1.3", "error": "Ignore on success"}, False),
    ({"ok": False, "version": "0.1.4", "current_version": "0.1.3", "error": {"execute": "never"}}, False),
    (["not", "an", "object"], False),
])
def test_previous_install_result_is_bounded_typed_and_bound_to_versions(tmp_path, report, expect_error):
    mgr, _, _ = manager(tmp_path)
    mgr.check()
    mgr.start_download()
    wait_download(mgr)
    (mgr.cache_dir / "install-result.json").write_text(json.dumps(report), encoding="utf-8-sig")
    restored = UpdateManager(mgr.cache_dir, "0.1.3", platform_key="win-x64", packaged=True,
                             executable=mgr.executable)
    assert restored.status()["status"] == "downloaded"
    assert (restored.status()["error_code"] == "install_failed") == expect_error
    if expect_error:
        assert restored.status()["error"] == report["error"]
    upgraded = UpdateManager(mgr.cache_dir, "0.1.4", platform_key="win-x64", packaged=True,
                            executable=mgr.executable)
    assert upgraded.status()["error"] == ""


def test_oversized_install_report_is_ignored(tmp_path):
    mgr, _, _ = manager(tmp_path)
    mgr.check()
    mgr.start_download()
    wait_download(mgr)
    (mgr.cache_dir / "install-result.json").write_text(" " * 16385)
    restored = UpdateManager(mgr.cache_dir, "0.1.3", platform_key="win-x64", packaged=True,
                             executable=mgr.executable)
    assert restored.status()["status"] == "downloaded"
    assert restored.status()["error"] == ""


def test_force_check_after_download_finds_newer_release(tmp_path):
    mgr, record, calls = manager(tmp_path)
    mgr.check()
    mgr.start_download()
    wait_download(mgr)
    record.clear()
    record.update(manifest(b"next installer", version="0.1.5"))
    assert mgr.check()["status"] == "downloaded"  # background check retains the ready package
    state = mgr.check(force=True)
    assert len(calls) == 3
    assert state["status"] == "available"
    assert state["version"] == "0.1.5"
    assert state["downloaded_bytes"] == 0
    assert mgr._package is None
    assert not (mgr.cache_dir / "verified-update.json").exists()
    assert not mgr.prepare_install()["ok"]


def test_force_check_of_same_release_keeps_verified_download(tmp_path):
    mgr, _, calls = manager(tmp_path)
    mgr.check()
    mgr.start_download()
    wait_download(mgr)
    package = mgr._package
    state = mgr.check(force=True)
    assert len(calls) == 3
    assert state["status"] == "downloaded"
    assert state["downloaded_bytes"] == state["total_bytes"]
    assert mgr._package == package
    assert mgr.prepare_install()["ok"]


def test_failed_force_check_keeps_ready_package_and_reports_error(tmp_path):
    mgr, _, _ = manager(tmp_path)
    mgr.check()
    mgr.start_download()
    wait_download(mgr)
    package, metadata = mgr._package, mgr._manifest
    def offline(request):
        raise httpx.ConnectError("offline", request=request)
    mgr._client = httpx.Client(transport=httpx.MockTransport(offline))
    state = mgr.check(force=True)
    assert state["status"] == "downloaded"
    assert state["version"] == "0.1.4"
    assert state["error_code"] == "check_failed"
    assert mgr._package == package
    assert mgr._manifest == metadata
    assert mgr.prepare_install()["ok"]


def test_download_failure_and_cancel_allow_retry(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Paused(httpx.SyncByteStream):
        def __iter__(self):
            entered.set()
            release.wait(2)
            yield b"installer"
    mgr, _, calls = manager(tmp_path, handler=lambda r: httpx.Response(200, stream=Paused()))
    mgr.check()
    mgr.start_download()
    assert entered.wait(2)
    mgr.start_download()  # does not create a second worker
    mgr.cancel_download()
    release.set()
    assert wait_download(mgr)["status"] == "available"
    assert len(calls) == 2
    assert not list(mgr.cache_dir.glob("*.part"))
    mgr._client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"installer")))
    mgr.start_download()
    assert wait_download(mgr)["status"] == "downloaded"
