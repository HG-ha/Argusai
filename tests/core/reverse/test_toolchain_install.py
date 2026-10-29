"""Managed reverse toolchain install (persistent under data_root/tools/reverse)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from argusai.core.reverse.toolchain import tool_unavailable_error
from argusai.core.reverse.toolchain_install import (
    _PIP_PACKAGES,
    _android_build_tools_zip_url,
    _android_platform_tools_zip_url,
    _pick_yara_release_asset,
    enrich_toolchain_scan_result,
    install_inventory,
    install_reverse_tool,
    installable_tools,
    persist_toolchain_paths,
)
from argusai.core.settings import AppSettings
from argusai.gui.settings import SettingsMixin


class _FakeSettingsMgr:
    def __init__(self, *, data_root: str = "", paths: dict[str, str] | None = None) -> None:
        self._settings = AppSettings(
            data_root=data_root or str(Path("C:/ArgusAI/data")),
            toolchain_paths=dict(paths or {}),
        )

    def get(self) -> AppSettings:
        return self._settings

    def save(self, settings: AppSettings) -> None:
        self._settings = settings


class _Api(SettingsMixin):
    def __init__(self, mgr: _FakeSettingsMgr) -> None:
        self.settings_mgr = mgr


def test_android_build_tools_zip_url_uses_r34_not_r34_0_0() -> None:
    for os_name, tag in (
        ("windows", "windows"),
        ("macos", "macosx"),
        ("linux", "linux"),
    ):
        url = _android_build_tools_zip_url(os_name)
        assert url == f"https://dl.google.com/android/repository/build-tools_r34-{tag}.zip"
        assert "r34.0.0" not in url


def test_android_platform_tools_zip_url_uses_darwin_on_macos() -> None:
    assert _android_platform_tools_zip_url("windows").endswith("platform-tools-latest-windows.zip")
    assert _android_platform_tools_zip_url("linux").endswith("platform-tools-latest-linux.zip")
    assert _android_platform_tools_zip_url("macos").endswith("platform-tools-latest-darwin.zip")
    assert "macos.zip" not in _android_platform_tools_zip_url("macos")


def test_capa_pip_package_is_flare_capa_not_capa() -> None:
    assert _PIP_PACKAGES["capa"] == ("flare-capa",)


@patch("argusai.core.reverse.toolchain_install._http_get_json")
def test_pick_yara_release_skips_source_only_latest(mock_json: MagicMock) -> None:
    mock_json.return_value = [
        {"tag_name": "v4.5.8", "assets": []},
        {
            "tag_name": "v4.5.5",
            "assets": [
                {
                    "name": "yara-4.5.5-2368-win64.zip",
                    "browser_download_url": "https://example.test/yara-win64.zip",
                }
            ],
        },
    ]
    asset = _pick_yara_release_asset("windows")
    assert asset["name"] == "yara-4.5.5-2368-win64.zip"


@patch("argusai.core.reverse.toolchain_install._http_get_json")
def test_pick_yara_release_errors_when_no_os_asset(mock_json: MagicMock) -> None:
    mock_json.return_value = [
        {"tag_name": "v4.5.8", "assets": []},
        {
            "tag_name": "v4.5.5",
            "assets": [{"name": "yara-4.5.5-2368-win64.zip", "browser_download_url": "x"}],
        },
    ]
    try:
        _pick_yara_release_asset("linux")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "linux" in str(exc)
        assert "Windows" in str(exc)


def test_install_inventory_marks_manual_tools() -> None:
    inv = install_inventory()
    by_tool = {row["tool"]: row for row in inv["tools"]}
    assert by_tool["jadx"]["can_install"] is True
    assert by_tool["ghidra"]["can_install"] is True
    assert by_tool["jarsigner"]["can_install"] is False


def test_enrich_toolchain_scan_result_adds_can_install() -> None:
    scan = {
        "ok": True,
        "tools": {
            "jadx": {"available": False, "path": ""},
            "ghidra": {"available": False, "path": ""},
            "jarsigner": {"available": False, "path": ""},
        },
    }
    out = enrich_toolchain_scan_result(scan, data_root=Path("C:/ArgusAI/data/tools"))
    assert out["tools"]["jadx"]["can_install"] is True
    assert out["tools"]["ghidra"]["can_install"] is True
    assert out["tools"]["jarsigner"]["can_install"] is False
    assert "reverse" in out["install_root"]


@patch("argusai.core.reverse.toolchain.detect_tool", return_value={"available": False, "path": ""})
def test_tool_unavailable_error_suggests_managed_install_for_jadx(_mock_detect: MagicMock) -> None:
    err = tool_unavailable_error("jadx")
    assert err["can_install"] is True
    assert err["next_step"] == "reverse_toolchain_install"
    assert "reverse_toolchain_install" in err["suggested_fix"]


def test_tool_unavailable_error_manual_for_jarsigner() -> None:
    err = tool_unavailable_error("jarsigner")
    assert err["can_install"] is False
    assert err["next_step"] == "reverse_toolchain_scan"


def test_tool_unavailable_error_managed_for_ghidra() -> None:
    err = tool_unavailable_error("ghidra")
    assert err["can_install"] is True
    assert err["next_step"] == "reverse_toolchain_install"


def test_installable_tools_enum_excludes_manual() -> None:
    enum = installable_tools()
    assert "jadx" in enum
    assert "frida" in enum
    assert "apktool" in enum
    assert "ghidra" in enum
    assert "jarsigner" not in enum
    assert "class-dump" not in enum


@patch("argusai.core.reverse.toolchain_install._install_jadx")
@patch("argusai.core.reverse.toolchain_install.clear_toolchain_cache")
@patch("argusai.core.reverse.toolchain_install.detect_tool")
def test_install_reverse_tool_persists_path(
    mock_detect: MagicMock,
    _mock_clear: MagicMock,
    mock_jadx: MagicMock,
    tmp_path: Path,
) -> None:
    exe = tmp_path / "jadx.bat"
    exe.write_text("@echo off\n", encoding="utf-8")
    mock_jadx.return_value = exe
    # First call (pre-install check) sees missing; verification after install sees available.
    mock_detect.side_effect = [
        {"available": False, "path": "", "name": "jadx"},
        {"available": True, "path": str(exe), "name": "jadx"},
    ]

    mgr = _FakeSettingsMgr()
    tools_dir = tmp_path / "tools"
    out = install_reverse_tool("jadx", settings_mgr=mgr, data_root=tools_dir)

    assert out["ok"] is True
    assert out.get("already_installed") is not True
    assert mgr.get().toolchain_paths["jadx"] == str(exe.resolve())
    assert out["paths_saved"]["jadx"] == str(exe.resolve())


@patch("argusai.core.reverse.toolchain_install._install_ghidra")
@patch("argusai.core.reverse.toolchain_install.clear_toolchain_cache")
@patch("argusai.core.reverse.toolchain_install.detect_tool")
def test_install_reverse_tool_ghidra_dispatch(
    mock_detect: MagicMock,
    _mock_clear: MagicMock,
    mock_ghidra: MagicMock,
    tmp_path: Path,
) -> None:
    hl = tmp_path / "ghidra" / "support" / "analyzeHeadless.bat"
    hl.parent.mkdir(parents=True, exist_ok=True)
    hl.write_text("", encoding="utf-8")
    mock_ghidra.return_value = hl
    mock_detect.side_effect = [
        {"available": False, "path": "", "name": "ghidra"},
        {"available": True, "path": str(hl), "name": "ghidra"},
    ]
    mgr = _FakeSettingsMgr()
    out = install_reverse_tool("ghidra", settings_mgr=mgr, data_root=tmp_path / "tools")
    assert out["ok"] is True
    assert mgr.get().toolchain_paths["ghidra"] == str(hl.resolve())
    mock_ghidra.assert_called_once()


@patch("argusai.core.reverse.toolchain_install._install_jadx")
@patch("argusai.core.reverse.toolchain_install.detect_tool")
def test_install_reverse_tool_skips_when_already_installed(
    mock_detect: MagicMock,
    mock_jadx: MagicMock,
    tmp_path: Path,
) -> None:
    mock_detect.return_value = {"available": True, "path": "C:\\PATH\\jadx.bat", "name": "jadx"}
    mgr = _FakeSettingsMgr()

    out = install_reverse_tool("jadx", settings_mgr=mgr, data_root=tmp_path / "tools")

    assert out["ok"] is True
    assert out["already_installed"] is True
    mock_jadx.assert_not_called()


@patch("argusai.core.reverse.toolchain_install._install_jadx")
@patch("argusai.core.reverse.toolchain_install.clear_toolchain_cache")
@patch("argusai.core.reverse.toolchain_install.detect_tool")
def test_install_reverse_tool_force_overrides_existing(
    mock_detect: MagicMock,
    _mock_clear: MagicMock,
    mock_jadx: MagicMock,
    tmp_path: Path,
) -> None:
    exe = tmp_path / "jadx.bat"
    exe.write_text("@echo off\n", encoding="utf-8")
    mock_jadx.return_value = exe
    # detect_tool is only called for post-install verification when force=True.
    mock_detect.return_value = {"available": True, "path": str(exe), "name": "jadx"}

    out = install_reverse_tool(
        "jadx",
        settings_mgr=_FakeSettingsMgr(),
        data_root=tmp_path / "tools",
        force=True,
    )

    assert out["ok"] is True
    assert out.get("already_installed") is not True
    mock_jadx.assert_called_once()


@patch("argusai.core.reverse.toolchain_install.install_reverse_tool")
@patch("argusai.core.reverse.toolchain.sync_path_overrides_from_settings")
@patch("argusai.core.reverse.toolchain.clear_toolchain_cache")
def test_gui_install_reverse_toolchain_delegates(
    _mock_clear: MagicMock,
    _mock_sync: MagicMock,
    mock_install: MagicMock,
) -> None:
    mock_install.return_value = {"ok": True, "tool": "frida", "path": "C:\\tools\\reverse\\venv\\Scripts\\frida.exe"}
    api = _Api(_FakeSettingsMgr())
    out = api.install_reverse_toolchain("frida")
    assert out["ok"] is True
    mock_install.assert_called_once()
    assert mock_install.call_args.args[0] == "frida"
    assert mock_install.call_args.kwargs.get("force") is False


def test_gui_install_reverse_toolchain_rejects_manual_tools() -> None:
    api = _Api(_FakeSettingsMgr())
    out = api.install_reverse_toolchain("jarsigner")
    assert out["ok"] is False
    assert "managed-install" in out["error"]


def test_persist_toolchain_paths_merges() -> None:
    mgr = _FakeSettingsMgr(paths={"jadx": "C:\\old\\jadx.bat"})
    persist_toolchain_paths(mgr, {"apktool": "C:\\new\\apktool.bat"})
    paths = mgr.get().toolchain_paths
    assert paths["jadx"] == "C:\\old\\jadx.bat"
    assert paths["apktool"] == "C:\\new\\apktool.bat"


def test_reverse_toolchain_install_executor_rejects_unknown_tool() -> None:
    from argusai.core.tools.native_reverse import reverse_toolchain_install

    api = _Api(_FakeSettingsMgr())
    out = reverse_toolchain_install(api, tool="jarsigner")
    assert out["ok"] is False
    assert out["do_not_retry"] is True
    assert "not a managed-install target" in out["error"]


def test_reverse_toolchain_install_executor_requires_tool() -> None:
    from argusai.core.tools.native_reverse import reverse_toolchain_install

    api = _Api(_FakeSettingsMgr())
    out = reverse_toolchain_install(api, tool="")
    assert out["ok"] is False
    assert "tool name required" in out["error"]


def test_reverse_toolchain_install_schema_required_and_enum() -> None:
    from argusai.core.assistant.tools import build_tool_registry

    registry = build_tool_registry(None)
    tool = registry._tools["reverse_toolchain_install"]
    assert "tool" in tool.required
    enum = tool.parameters["tool"].get("enum") or []
    assert "jadx" in enum
    assert "frida" in enum
    assert "ghidra" in enum
    assert "jarsigner" not in enum
    assert "force" in tool.parameters
    assert tool.parameters["force"].get("default") is False


def test_reverse_toolchain_install_session_block_includes_install_root() -> None:
    from argusai.core.assistant.session_blocks import _reverse_result_block

    ok_result = {
        "ok": True,
        "tool": "jadx",
        "path": "C:\\data\\tools\\reverse\\jadx\\bin\\jadx.bat",
        "install_root": "C:\\data\\tools\\reverse",
        "paths_saved": {"jadx": "C:\\data\\tools\\reverse\\jadx\\bin\\jadx.bat"},
        "duration_ms": 12000,
        "verified": True,
    }
    block = _reverse_result_block("reverse_toolchain_install", ok_result)
    assert block["type"] == "reverse_result"
    assert block["install_root"] == "C:\\data\\tools\\reverse"
    assert block["paths_saved"]["jadx"].endswith("jadx.bat")
    assert "Installed jadx" in block["summary"]
    assert block["already_installed"] is False

    err_result = {"ok": False, "tool": "frida", "error": "network unreachable", "docs_url": "https://frida.re"}
    err_block = _reverse_result_block("reverse_toolchain_install", err_result)
    assert "failed" in err_block["summary"]
    assert err_block["docs_url"] == "https://frida.re"
