"""Tests for settings toolchain path autofill."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from argusai.gui.settings import SettingsMixin


class _FakeSettingsMgr:
    def __init__(self, paths: dict[str, str] | None = None) -> None:
        self._paths = dict(paths or {})

    def get(self):
        obj = MagicMock()
        obj.toolchain_paths = dict(self._paths)
        return obj


class _Api(SettingsMixin):
    def __init__(self, paths: dict[str, str] | None = None) -> None:
        self.settings_mgr = _FakeSettingsMgr(paths)


@patch("argusai.core.reverse.toolchain.sync_path_overrides_from_settings")
@patch("argusai.core.reverse.toolchain.clear_toolchain_cache")
@patch("argusai.core.reverse.toolchain.detect_tool")
def test_scan_toolchain_paths_suggests_only_blanks(
    mock_detect: MagicMock,
    _mock_clear: MagicMock,
    _mock_sync: MagicMock,
) -> None:
    api = _Api({"jadx": "C:\\custom\\jadx.bat"})

    def _detect(name: str) -> dict[str, object]:
        mapping = {
            "jadx": {"available": True, "path": "C:\\PATH\\jadx.bat"},
            "apktool": {"available": True, "path": "C:\\PATH\\apktool.bat"},
            "frida": {"available": False, "path": "", "error": "not found"},
        }
        return {"name": name, **mapping.get(name, {"available": False, "path": ""})}

    mock_detect.side_effect = _detect
    out = api.scan_toolchain_paths()
    assert out["ok"] is True
    assert "jadx" not in out["suggestions"]
    assert out["suggestions"]["apktool"] == "C:\\PATH\\apktool.bat"
    assert "frida" not in out["suggestions"]
    assert out["tools"]["frida"]["can_install"] is True
    assert out["tools"]["ghidra"]["can_install"] is True
    assert out["tools"]["jarsigner"]["can_install"] is False
    assert "install_root" in out


def test_sandbox_runtime_status_exposes_managed_health_and_sources(tmp_path) -> None:
    settings = SimpleNamespace(
        data_root=str(tmp_path),
        runtime_mirrors={"python": "https://mirror.example/python"},
    )
    api = _Api()
    api.settings_mgr.get = MagicMock(return_value=settings)
    managed = {
        "ok": True,
        "root": str(tmp_path / "tools" / "runtimes"),
        "python": {"runtime": "python", "healthy": False},
        "node": {"runtime": "node", "healthy": True},
        "wabt": {"runtime": "wabt", "healthy": False},
    }

    with patch("argusai.core.managed_runtimes.all_runtime_status", return_value=managed):
        result = api.sandbox_runtime_status()

    assert result["ok"] is True
    assert result["python"]["download_sources"][0].startswith("https://mirror.example/python")
    assert result["node"]["npm_registries"][0] == "https://registry.npmjs.org"
    assert "WebAssembly/wabt" in result["wabt"]["download_sources"][0]


def test_install_sandbox_runtime_accepts_wabt(tmp_path) -> None:
    settings = SimpleNamespace(data_root=str(tmp_path), runtime_mirrors={})
    api = _Api()
    api.settings_mgr.get = MagicMock(return_value=settings)

    with patch(
        "argusai.core.managed_runtimes.install_runtime",
        return_value={"ok": True, "runtime": "wabt"},
    ) as install:
        result = api.install_sandbox_runtime("wabt")

    assert result["ok"] is True
    assert install.call_args.args[0] == "wabt"


def test_install_sandbox_runtime_rejects_unknown_runtime(tmp_path) -> None:
    settings = SimpleNamespace(data_root=str(tmp_path), runtime_mirrors={})
    api = _Api()
    api.settings_mgr.get = MagicMock(return_value=settings)

    result = api.install_sandbox_runtime("rizin")

    assert result["ok"] is False
    assert result["error_code"] == "invalid_runtime"


def test_install_sandbox_runtime_passes_settings_mirrors(tmp_path) -> None:
    settings = SimpleNamespace(
        data_root=str(tmp_path),
        runtime_mirrors={"node": "https://mirror.example/node"},
    )
    api = _Api()
    api.settings_mgr.get = MagicMock(return_value=settings)

    with patch(
        "argusai.core.managed_runtimes.install_runtime",
        return_value={"ok": True, "runtime": "node"},
    ) as install:
        result = api.install_sandbox_runtime("node", True)

    assert result["ok"] is True
    assert install.call_args.kwargs["force"] is True
    assert install.call_args.kwargs["mirrors"] == settings.runtime_mirrors
