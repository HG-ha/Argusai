"""URL / platform mapping for vendoring innoextract via scripts/vendor_runtime.py."""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "vendor_runtime.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("vendor_runtime_ie_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "plat,fragment",
    [
        ("win-x64", "windows.zip"),
        ("linux-x64", "linux.tar.xz"),
    ],
)
def test_innoextract_url_for_portable_platforms(monkeypatch, plat, fragment):
    mod = _load_module()
    monkeypatch.setattr(mod, "_platform_subdir", lambda: plat)
    res = mod._innoextract_archive_url(mod.INNOEXTRACT_VERSION)
    assert res is not None
    url, fn = res
    assert fragment in fn
    assert url.endswith(fn)
    assert mod.INNOEXTRACT_VERSION in url
    assert url.startswith("https://github.com/dscharrer/innoextract/releases/download/")


@pytest.mark.parametrize("plat", ["macos-arm64", "macos-x64", "linux-arm64", "win-arm64"])
def test_innoextract_url_none_for_unsupported_platforms(monkeypatch, plat):
    mod = _load_module()
    monkeypatch.setattr(mod, "_platform_subdir", lambda: plat)
    assert mod._innoextract_archive_url(mod.INNOEXTRACT_VERSION) is None
