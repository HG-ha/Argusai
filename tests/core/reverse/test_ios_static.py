"""Tests for iOS static helpers and class-dump bridge."""

from __future__ import annotations

import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from argusai.core.reverse.ios_static import analyze_ipa_path, extract_main_binary
from argusai.core.tools.native_reverse import ios_class_dump


@pytest.fixture
def mini_ipa(tmp_path: Path) -> Path:
    path = tmp_path / "demo.ipa"
    plist = b"""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>com.example.demo</string>
<key>CFBundleExecutable</key><string>Demo</string>
</dict></plist>"""
    binary = b"\xcf\xfa\xed\xfe" + b"\x00" * 32 + b"https://mobile.example/api\x00"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Demo.app/Info.plist", plist)
        zf.writestr("Payload/Demo.app/Demo", binary)
    return path


def test_extract_main_binary_writes_file(mini_ipa: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "bin"
    extracted = extract_main_binary(mini_ipa, out_dir)
    assert extracted["ok"] is True
    path = Path(extracted["path"])
    assert path.is_file()
    assert path.read_bytes().startswith(b"\xcf\xfa\xed\xfe")


@patch("argusai.core.tools.native_reverse.run_class_dump")
@patch("argusai.core.tools.native_reverse.extract_main_binary")
@patch(
    "argusai.core.tools.native_reverse.detect_tool",
    return_value={"available": False, "path": "", "error": "not found on PATH"},
)
def test_ios_class_dump_preflight_blocks_without_tool(
    _mock_detect: MagicMock,
    mock_extract: MagicMock,
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    ipa = tmp_path / "sample.ipa"
    ipa.write_bytes(b"PK")
    api = type("Api", (), {"settings_mgr": type("SM", (), {"get": lambda self: {}})()})()

    out = ios_class_dump(api, path=str(ipa))

    assert out["ok"] is False
    assert out.get("blocked") is True
    mock_extract.assert_not_called()
    mock_run.assert_not_called()
