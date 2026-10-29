"""Tests for cross-platform toolchain path resolution."""

from __future__ import annotations

def test_settings_toolchain_keys_include_ios_and_binary() -> None:
    from argusai.core.reverse.toolchain_settings import SETTINGS_TOOLCHAIN_KEYS

    assert "class-dump" in SETTINGS_TOOLCHAIN_KEYS
    assert "ghidra" in SETTINGS_TOOLCHAIN_KEYS
    assert "innoextract" in SETTINGS_TOOLCHAIN_KEYS
    assert "jarsigner" in SETTINGS_TOOLCHAIN_KEYS
