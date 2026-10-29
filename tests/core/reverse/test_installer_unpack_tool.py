"""Registration and wiring for the installer_unpack tool."""

from argusai.core.reverse.toolchain_install import _INSTALL_SPECS, installable_tools
from argusai.core.reverse.toolchain_settings import SETTINGS_TOOLCHAIN_KEYS
from argusai.core.tools import EXECUTORS
from argusai.core.assistant.skills import EXPLICIT_SCHEMAS, TOOL_GROUPS


def test_installer_unpack_is_registered_as_a_tool():
    assert "installer_unpack" in EXECUTORS
    assert callable(EXECUTORS["installer_unpack"])


def test_installer_unpack_has_schema_and_groups():
    schema = EXPLICIT_SCHEMAS["installer_unpack"]
    for key in ("path", "password", "list_only", "output_dir"):
        assert key in schema
    assert "installer_unpack" in TOOL_GROUPS["binary"]
    assert "installer_unpack" in TOOL_GROUPS["reverse"]


def test_innoextract_is_installable_and_in_settings():
    assert "innoextract" in _INSTALL_SPECS
    assert _INSTALL_SPECS["innoextract"].method == "innoextract_zip"
    assert "innoextract" in installable_tools()
    assert "innoextract" in SETTINGS_TOOLCHAIN_KEYS
