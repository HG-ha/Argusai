"""Tests for external toolchain detection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from argusai.core.reverse import toolchain


def test_detect_unknown_tool() -> None:
    toolchain.clear_toolchain_cache()
    info = toolchain.detect_tool("not-a-real-tool-name-xyz")
    assert info["available"] is False


def test_scan_toolchain_structure() -> None:
    toolchain.clear_toolchain_cache()
    result = toolchain.scan_toolchain(names=["frida", "jadx"])
    assert result["ok"] is True
    assert "frida" in result["tools"]
    assert "jadx" in result["tools"]
    assert isinstance(result["available"], list)
    assert isinstance(result["missing"], list)


@patch(
    "argusai.core.reverse.toolchain.detect_tool",
    side_effect=lambda name: {"available": name == "apktool", "path": "", "error": ""},
)
def test_tool_unavailable_error_shape(_mock_detect: MagicMock) -> None:
    toolchain.clear_toolchain_cache()
    out = toolchain.tool_unavailable_error(
        "jadx",
        info={"available": False, "path": "", "error": "not found on PATH"},
    )
    assert out["ok"] is False
    assert out["blocked"] is True
    assert out["do_not_retry"] is True
    assert out["toolchain_required"] == "jadx"
    assert "jadx not installed" in out["error"]
    assert out["next_step"] == "android_apktool_decode"
    assert out["can_install"] is True
    assert out["alternatives"] == [{"cli": "apktool", "tool": "android_apktool_decode"}]
