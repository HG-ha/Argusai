"""Tests for JADX / APKTool CLI bridge."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from typing import Any

from argusai.core.reverse import android_toolchain, toolchain


@patch("argusai.core.reverse.android_toolchain.detect_tool", return_value={"available": False, "path": "", "error": "missing"})
def test_run_jadx_missing_tool(_mock: MagicMock) -> None:
    out = android_toolchain.run_jadx_decompile(Path("x.apk"), Path("out"))
    assert out["ok"] is False
    assert out.get("blocked") is True
    assert out.get("do_not_retry") is True
    assert "jadx not installed" in out["error"]


@patch("argusai.core.reverse.android_toolchain.detect_tool", return_value={"available": True, "path": "/bin/jadx", "version": "1.5.0"})
@patch("argusai.core.reverse.android_toolchain._run_cli")
def test_run_jadx_success(mock_run: MagicMock, _mock_tool: MagicMock, tmp_path: Path) -> None:
    apk = tmp_path / "sample.apk"
    apk.write_bytes(b"PK")
    out_dir = tmp_path / "jadx-out"
    mock_run.return_value = {"ok": True, "command": "jadx", "returncode": 0, "stdout": "", "stderr": "", "error": ""}
    (out_dir / "sources").mkdir(parents=True)
    (out_dir / "sources" / "Main.java").write_text("class Main {}", encoding="utf-8")
    (out_dir / "resources").mkdir(parents=True)
    (out_dir / "resources" / "AndroidManifest.xml").write_text("<manifest/>", encoding="utf-8")

    result = android_toolchain.run_jadx_decompile(apk, out_dir, timeout_seconds=30)
    assert result["ok"] is True
    assert result["java_file_count"] == 1
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "/bin/jadx"
    assert "-d" in cmd
    assert str(apk) in cmd


@patch("argusai.core.reverse.android_toolchain.detect_tool", return_value={"available": True, "path": "/bin/apktool", "version": "2.9.0"})
@patch("argusai.core.reverse.android_toolchain._run_cli")
def test_run_apktool_success(mock_run: MagicMock, _mock_tool: MagicMock, tmp_path: Path) -> None:
    apk = tmp_path / "sample.apk"
    apk.write_bytes(b"PK")
    out_dir = tmp_path / "apktool-out"
    mock_run.return_value = {"ok": True, "command": "apktool", "returncode": 0, "stdout": "", "stderr": "", "error": ""}
    out_dir.mkdir()
    (out_dir / "smali").mkdir()
    (out_dir / "smali" / "a.smali").write_text(".class", encoding="utf-8")
    (out_dir / "AndroidManifest.xml").write_text("<manifest/>", encoding="utf-8")

    result = android_toolchain.run_apktool_decode(apk, out_dir, timeout_seconds=30)
    assert result["ok"] is True
    assert result["smali_file_count"] == 1
    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == ["/bin/apktool", "d", str(apk)]


@patch("argusai.core.reverse.android_toolchain._run_cli")
def test_run_apktool_decode_partial_on_timeout(mock_run: MagicMock, tmp_path: Path) -> None:
    apk = tmp_path / "sample.apk"
    apk.write_bytes(b"PK")
    out_dir = tmp_path / "apktool-out"
    out_dir.mkdir()
    (out_dir / "AndroidManifest.xml").write_text("<manifest/>", encoding="utf-8")
    (out_dir / "smali").mkdir()
    (out_dir / "smali" / "a.smali").write_text(".class", encoding="utf-8")
    mock_run.return_value = {
        "ok": False,
        "timed_out": True,
        "error": "command timed out after 30 seconds",
        "command": "apktool d",
        "returncode": -1,
        "stdout": "",
        "stderr": "",
    }
    with patch("argusai.core.reverse.android_toolchain._tool_executable", return_value=("/bin/apktool", {"available": True})):
        result = android_toolchain.run_apktool_decode(apk, out_dir, timeout_seconds=30)
    assert result.get("partial") is True
    assert result.get("usable_output") is True
    assert result.get("retryable") is False


@patch("argusai.core.reverse.android_toolchain._tool_executable")
@patch("argusai.core.reverse.android_toolchain._run_cli")
def test_run_apktool_build_success(mock_run: MagicMock, mock_tool: MagicMock, tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "AndroidManifest.xml").write_text("<manifest/>", encoding="utf-8")
    out_apk = tmp_path / "out.apk"

    def _tool(name: str) -> tuple[str | None, dict[str, Any]]:
        if name == "apktool":
            return "/bin/apktool", {"available": True, "path": "/bin/apktool", "version": "2.9.0"}
        return None, {"available": False}

    mock_tool.side_effect = _tool

    def _produce_apk(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        _write_minimal_apk(out_apk, with_meta_inf=False)
        return {"ok": True, "command": "apktool b", "returncode": 0, "stdout": "", "stderr": "", "error": ""}

    mock_run.side_effect = _produce_apk

    result = android_toolchain.run_apktool_build(project, out_apk, timeout_seconds=60)
    assert result["ok"] is True
    assert result.get("validation", {}).get("ok") is True
    cmd = mock_run.call_args_list[0][0][0]
    assert cmd[:4] == ["/bin/apktool", "b", str(project), "-o"]


@patch("argusai.core.reverse.android_toolchain._tool_executable")
@patch("argusai.core.reverse.android_toolchain._run_cli")
def test_run_apk_sign_apksigner(mock_run: MagicMock, mock_tool: MagicMock, tmp_path: Path) -> None:
    unsigned = tmp_path / "unsigned.apk"
    _write_minimal_apk(unsigned)
    signed = tmp_path / "signed.apk"
    ks = tmp_path / "debug.keystore"
    ks.write_bytes(b"ks")

    def _tool(name: str) -> tuple[str | None, dict[str, Any]]:
        if name == "apksigner":
            return "/bin/apksigner", {"available": True}
        return None, {"available": False}

    mock_tool.side_effect = _tool

    def _cli(cmd: list[str], **_kwargs: Any) -> dict[str, Any]:
        if cmd and cmd[0] == "/bin/apksigner" and "sign" in cmd:
            _write_minimal_apk(signed, with_meta_inf=True)
            return {"ok": True, "command": "apksigner sign", "returncode": 0, "stdout": "", "stderr": "", "error": ""}
        if cmd and cmd[0] == "/bin/apksigner" and "verify" in cmd:
            return {"ok": True, "command": "apksigner verify", "returncode": 0, "stdout": "Verified", "stderr": "", "error": ""}
        return {"ok": True, "command": " ".join(cmd), "returncode": 0, "stdout": "", "stderr": "", "error": ""}

    mock_run.side_effect = _cli

    result = android_toolchain.run_apk_sign(
        unsigned,
        signed,
        keystore_path=str(ks),
        keystore_pass="secret",
        key_alias="mykey",
        timeout_seconds=60,
    )
    assert result["ok"] is True
    assert result["tool"] == "apksigner"
    assert result.get("validation", {}).get("ok") is True
    assert mock_run.call_count >= 2


def _write_minimal_apk(
    path: Path,
    *,
    with_meta_inf: bool = True,
    stored_so: bool = False,
    compressed_so: bool = False,
) -> None:
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("AndroidManifest.xml", b"<manifest package='com.example'/>")
        if with_meta_inf:
            zf.writestr("META-INF/CERT.SF", b"Signature-Version: 1.0\n")
            zf.writestr("META-INF/CERT.RSA", b"rsa")
        if stored_so:
            info = zipfile.ZipInfo("lib/arm64-v8a/libfoo.so")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, b"\x7fELF" + b"\0" * 32)
        if compressed_so:
            info = zipfile.ZipInfo("lib/arm64-v8a/libbar.so")
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, b"\x7fELF" + b"x" * 64)


def test_validate_apk_package_flags_compressed_native_libs(tmp_path: Path) -> None:
    apk = tmp_path / "bad.apk"
    _write_minimal_apk(apk, compressed_so=True)
    with patch("argusai.core.reverse.android_toolchain._tool_executable", return_value=(None, {"available": False})):
        result = android_toolchain.validate_apk_package(apk, require_signature=False)
    assert result["ok"] is False
    assert result["installable_hint"] == "unlikely"
    assert any("ZIP-compressed" in e for e in result["errors"])


def test_validate_apk_package_ok_with_stored_libs(tmp_path: Path) -> None:
    apk = tmp_path / "good.apk"
    _write_minimal_apk(apk, stored_so=True)
    with patch("argusai.core.reverse.android_toolchain._tool_executable", return_value=(None, {"available": False})):
        result = android_toolchain.validate_apk_package(apk, require_signature=True)
    assert result["ok"] is True
    assert result["checks"]["zip_layout"]["stored_lib_count"] == 1


def test_toolchain_custom_path_override(tmp_path: Path) -> None:
    fake = tmp_path / "jadx-custom.bat"
    fake.write_text("@echo off", encoding="utf-8")
    toolchain.clear_toolchain_cache()
    toolchain.set_path_overrides({"jadx": str(fake)})

    with patch("argusai.core.reverse.toolchain._run_version", return_value=("jadx 1.0", "")):
        info = toolchain.detect_tool("jadx")

    assert info["available"] is True
    assert info["path"] == str(fake.resolve())
    toolchain.set_path_overrides({})


@patch("argusai.core.reverse.android_toolchain._tool_executable", return_value=(None, {"available": False}))
def test_run_objection_missing_tool(_mock: MagicMock) -> None:
    out = android_toolchain.run_objection_command("com.example.app", "android sslpinning disable")
    assert out["ok"] is False
    assert "objection not installed" in out["error"]


@patch("argusai.core.reverse.android_toolchain._tool_executable", return_value=("/bin/objection", {"available": True}))
@patch("argusai.core.reverse.android_toolchain._run_cli")
def test_run_objection_success(mock_run: MagicMock, _mock_tool: MagicMock) -> None:
    mock_run.return_value = {
        "ok": True,
        "command": "objection",
        "returncode": 0,
        "stdout": "SSL pinning disabled\n",
        "stderr": "",
        "error": "",
    }
    result = android_toolchain.run_objection_command(
        "com.example.app",
        "android sslpinning disable",
        network=True,
        timeout_seconds=30,
    )
    assert result["ok"] is True
    assert "SSL pinning" in result["output"]
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "/bin/objection"
    assert "-N" in cmd


@patch("argusai.core.reverse.android_toolchain._tool_executable", return_value=(None, {"available": False}))
def test_run_adb_devices_missing_tool(_mock: MagicMock) -> None:
    out = android_toolchain.run_adb_devices()
    assert out["ok"] is False
    assert "adb not installed" in out["error"]


@patch("argusai.core.reverse.android_toolchain._tool_executable", return_value=("/bin/adb", {"available": True}))
@patch("argusai.core.reverse.android_toolchain._run_cli")
def test_run_adb_devices_success(mock_run: MagicMock, _mock_tool: MagicMock) -> None:
    mock_run.return_value = {
        "ok": True,
        "command": "adb devices -l",
        "returncode": 0,
        "stdout": "List of devices attached\nemulator-5554 device\n",
        "stderr": "",
        "error": "",
    }
    out = android_toolchain.run_adb_devices()
    assert out["ok"] is True
    assert "emulator-5554" in out["devices_text"]


@patch("argusai.core.reverse.android_toolchain._tool_executable", return_value=("/bin/adb", {"available": True}))
@patch("argusai.core.reverse.android_toolchain._run_cli")
def test_run_adb_logcat_dump_mode(mock_run: MagicMock, _mock_tool: MagicMock) -> None:
    mock_run.return_value = {
        "ok": True,
        "command": "adb logcat -d",
        "returncode": 0,
        "stdout": "01-01 00:00:00.000 I/Test: hello\n",
        "stderr": "",
        "error": "",
    }
    out = android_toolchain.run_adb_logcat(duration_seconds=0, max_lines=100)
    assert out["ok"] is True
    assert out["mode"] == "dump"
    assert out["line_count"] == 1
