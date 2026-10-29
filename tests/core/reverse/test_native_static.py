"""Static analysis unit tests for mobile/native modules."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from argusai.core.reverse.android_static import analyze_apk_path
from argusai.core.reverse.ios_static import analyze_ipa_path
from argusai.core.reverse.windows_static import analyze_pe_bytes
from unittest.mock import MagicMock, patch

from argusai.core.tools.native_reverse import android_jadx_decompile, reverse_toolchain_scan


@pytest.fixture
def mini_apk(tmp_path: Path) -> Path:
    path = tmp_path / "sample.apk"
    manifest = (
        b"\x00\x03" + b"com.example.demo\x00" * 2
        + b"android.permission.INTERNET\x00"
        + b"android.permission.CAMERA\x00"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("AndroidManifest.xml", manifest)
        zf.writestr("classes.dex", b"dex\n035\x00")
        zf.writestr("lib/arm64-v8a/libnative.so", b"\x7fELF")
    return path


@pytest.fixture
def mini_ipa(tmp_path: Path) -> Path:
    path = tmp_path / "sample.ipa"
    plist = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.example.demo</string>
<key>CFBundleExecutable</key><string>Demo</string>
<key>CFBundleShortVersionString</key><string>1.0</string>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsArbitraryLoads</key><true/></dict>
</dict></plist>"""
    binary = b"\xcf\xfa\xed\xfe" + b"\x00" * 64 + b"https://api.example.com/v1/login\x00"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Demo.app/Info.plist", plist)
        zf.writestr("Payload/Demo.app/Demo", binary)
        zf.writestr("Payload/Demo.app/Frameworks/Foo.framework/Foo", b"\xcf\xfa\xed\xfe")
    return path


def test_analyze_apk_finds_permissions_and_native(mini_apk: Path) -> None:
    out = analyze_apk_path(mini_apk)
    assert out["ok"] is True
    assert out["layout"]["dex_count"] == 1
    assert out["layout"]["native_lib_count"] == 1
    perms = out["manifest"]["permissions"]
    assert "android.permission.INTERNET" in perms
    assert any(f["tag"] == "native_code" for f in out["findings"])


def test_analyze_ipa_reads_bundle_and_ats(mini_ipa: Path) -> None:
    out = analyze_ipa_path(mini_ipa)
    assert out["ok"] is True
    summary = out["info_plist"]["summary"]
    assert summary["bundle_id"] == "com.example.demo"
    assert summary["executable"] == "Demo"
    assert out["layout"]["framework_count"] == 1
    assert out["main_binary"]["entry"].endswith("/Demo")
    assert "https://api.example.com/v1/login" in (out["main_binary"].get("urls") or [])
    assert any(f["tag"] == "ats_disabled" for f in out["findings"])
    assert any(f["tag"] == "binary_urls" for f in out["findings"])


def test_analyze_pe_rejects_non_pe() -> None:
    out = analyze_pe_bytes(b"not a pe file")
    assert out["ok"] is False


def test_reverse_toolchain_scan_shape() -> None:
    out = reverse_toolchain_scan(None, tools=["frida", "jadx"])
    assert out["ok"] is True
    assert "frida" in out["tools"]
    assert isinstance(out["missing"], list)


@patch("argusai.core.tools.native_reverse.run_jadx_decompile")
@patch(
    "argusai.core.tools.native_reverse.detect_tool",
    return_value={"available": False, "path": "", "error": "not found on PATH"},
)
def test_android_jadx_decompile_preflight_blocks_without_jadx(
    _mock_detect: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    apk = tmp_path / "sample.apk"
    apk.write_bytes(b"PK")
    api = type("Api", (), {"settings_mgr": type("SM", (), {"get": lambda self: {}})()})()

    out = android_jadx_decompile(api, path=str(apk))

    assert out["ok"] is False
    assert out.get("blocked") is True
    assert out.get("do_not_retry") is True
    assert "jadx not installed" in out["error"]
    mock_run.assert_not_called()
