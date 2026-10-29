"""IDA probe: headless-first candidates, override-driven resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from argusai.core.reverse import toolchain
from argusai.core.reverse.toolchain_install import install_inventory, installable_tools
from argusai.core.reverse.toolchain_settings import SETTINGS_TOOLCHAIN_KEYS


@pytest.fixture(autouse=True)
def _clean_probe_state() -> None:
    toolchain.set_path_overrides({})
    yield
    toolchain.set_path_overrides({})


def _probe() -> toolchain.ToolProbe:
    return next(p for p in toolchain.TOOL_PROBES if p.name == "ida")


def test_ida_probe_prefers_headless_binaries() -> None:
    probe = _probe()
    commands = probe.commands
    assert commands[0].startswith("idat")
    # Every headless candidate must be tried before any GUI binary.
    first_gui = min(i for i, name in enumerate(commands) if not name.startswith("idat"))
    assert all(commands[i].startswith("idat") for i in range(first_gui))
    assert set(commands) == {
        "idat64",
        "idat64.exe",
        "idat",
        "idat.exe",
        "ida64",
        "ida64.exe",
        "ida",
        "ida.exe",
    }


def test_ida_probe_is_optional_and_skips_version_execution() -> None:
    probe = _probe()
    assert probe.optional is True
    # IDA has no version flag; a stray argument would open the GUI.
    assert probe.version_args == ()


def test_configured_override_resolves_without_path_lookup(tmp_path: Path) -> None:
    headless = tmp_path / "IDA Professional 9.0" / "idat64.exe"
    headless.parent.mkdir(parents=True)
    headless.write_bytes(b"stub")
    toolchain.set_path_overrides({"ida": str(headless)})

    with patch("argusai.core.reverse.toolchain.shutil.which", return_value=None):
        info = toolchain.detect_tool("ida")

    assert info["available"] is True
    assert info["path"] == str(headless.resolve())
    assert info["version"] == ""
    assert info["error"] == ""


def test_missing_override_reports_actionable_hint() -> None:
    with patch("argusai.core.reverse.toolchain.shutil.which", return_value=None):
        info = toolchain.detect_tool("ida")

    assert info["available"] is False
    assert info["optional"] is True
    assert "settings.toolchain.paths.ida" in info["error"]
    assert "idat" in info["error"]


def test_broken_override_reports_configured_path_not_found(tmp_path: Path) -> None:
    toolchain.set_path_overrides({"ida": str(tmp_path / "gone" / "idat64.exe")})

    with patch("argusai.core.reverse.toolchain.shutil.which", return_value=None):
        info = toolchain.detect_tool("ida")

    assert info["available"] is False
    assert info["error"] == "configured path not found"


def test_scan_toolchain_includes_ida() -> None:
    with patch("argusai.core.reverse.toolchain.shutil.which", return_value=None):
        scan = toolchain.scan_toolchain()
    assert "ida" in scan["tools"]
    assert "ida" in scan["missing"]


def test_ida_is_a_settings_path_key_but_not_auto_installable() -> None:
    assert "ida" in SETTINGS_TOOLCHAIN_KEYS
    assert "ida" not in installable_tools()
    row = next(item for item in install_inventory()["tools"] if item["tool"] == "ida")
    assert row["can_install"] is False
    assert row["docs_url"].startswith("https://hex-rays.com")
    assert "settings.toolchain.paths.ida" in row["notes"]


def test_tool_unavailable_error_points_at_manual_install() -> None:
    with patch("argusai.core.reverse.toolchain.shutil.which", return_value=None):
        err = toolchain.tool_unavailable_error("ida")

    assert err["can_install"] is False
    assert err["next_step"] == "reverse_toolchain_scan"
    assert "settings.toolchain.paths.ida" in err["suggested_fix"]
    assert err["docs_url"].startswith("https://hex-rays.com")
