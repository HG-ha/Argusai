"""Vendor-aware reverse toolchain detection."""

import sys

from argusai.core.reverse import runtime_manager, toolchain


def test_vendor_lookup_only_applies_to_vendored_tools(monkeypatch, tmp_path):
    # A tool not in the vendored map must never resolve into the vendor tree.
    monkeypatch.setattr(runtime_manager, "_platform_subdir", lambda: "test-plat")
    monkeypatch.setattr(runtime_manager, "_vendor_dir", lambda: tmp_path)
    assert toolchain._vendor_tool_path("ghidra") is None


def test_innoextract_is_a_vendored_tool(monkeypatch, tmp_path):
    assert "innoextract" in toolchain._VENDORED_REVERSE_TOOLS
    plat = "test-plat"
    monkeypatch.setattr(runtime_manager, "_platform_subdir", lambda: plat)
    monkeypatch.setattr(runtime_manager, "_vendor_dir", lambda: tmp_path)
    exe = "innoextract.exe" if sys.platform.startswith("win") else "innoextract"
    binpath = tmp_path / "innoextract" / plat / exe
    binpath.parent.mkdir(parents=True, exist_ok=True)
    binpath.write_text("x", encoding="utf-8")
    toolchain.set_path_overrides({})
    toolchain.clear_toolchain_cache()
    info = toolchain.detect_tool("innoextract")
    assert info["available"] is True
    assert info["path"] == str(binpath.resolve())
    toolchain.clear_toolchain_cache()
