"""Verified vendoring of innoextract via scripts/vendor_runtime.py."""

import hashlib
import importlib.util
import urllib.error
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "vendor_runtime.py"


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.sent = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int) -> bytes:
        if self.sent:
            return b""
        self.sent = True
        return self.payload


def _load_module():
    spec = importlib.util.spec_from_file_location("vendor_runtime_ie_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(
    "plat,fragment,expected_sha256",
    [
        (
            "win-x64",
            "windows.zip",
            "6989342c9b026a00a72a38f23b62a8e6a22cc5de69805cf47d68ac2fec993065",
        ),
        (
            "linux-x64",
            "linux.tar.xz",
            "008efe5011476ccc4aae17c3e22038b5a1bc5c7aad2b9d4d869537bf3874d21f",
        ),
    ],
)
def test_innoextract_spec_for_portable_platforms(
    monkeypatch,
    plat,
    fragment,
    expected_sha256,
):
    mod = _load_module()
    monkeypatch.setattr(mod, "_platform_subdir", lambda: plat)
    res = mod._innoextract_archive_spec(mod.INNOEXTRACT_VERSION)
    assert res is not None
    urls, fn, sha256 = res
    assert fragment in fn
    assert sha256 == expected_sha256
    assert urls[0] == f"https://constexpr.org/innoextract/files/innoextract-1.9/{fn}"
    assert urls[1] == f"https://github.com/dscharrer/innoextract/releases/download/1.9/{fn}"


@pytest.mark.parametrize("plat", ["macos-arm64", "macos-x64", "linux-arm64", "win-arm64"])
def test_innoextract_url_none_for_unsupported_platforms(monkeypatch, plat):
    mod = _load_module()
    monkeypatch.setattr(mod, "_platform_subdir", lambda: plat)
    assert mod._innoextract_archive_spec(mod.INNOEXTRACT_VERSION) is None


def test_download_retries_transient_504_before_atomic_success(tmp_path: Path) -> None:
    mod = _load_module()
    payload = b"verified vendor archive"
    expected = hashlib.sha256(payload).hexdigest()
    calls: list[str] = []
    sleeps: list[float] = []

    def opener(request, *, timeout):
        assert timeout == 120
        calls.append(request.full_url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(request.full_url, 504, "timeout", {}, None)
        return _Response(payload)

    destination = tmp_path / "runtime.zip"
    source = mod._download(
        ["https://official.invalid/runtime.zip", "https://mirror.invalid/runtime.zip"],
        destination,
        label="runtime",
        expected_sha256=expected,
        opener=opener,
        sleeper=sleeps.append,
    )

    assert source == "https://official.invalid/runtime.zip"
    assert calls == ["https://official.invalid/runtime.zip"] * 3
    assert sleeps == [1.0, 2.0]
    assert destination.read_bytes() == payload
    assert not destination.with_name("runtime.zip.part").exists()


def test_download_falls_back_after_missing_official_source(tmp_path: Path) -> None:
    mod = _load_module()
    payload = b"verified vendor archive"
    expected = hashlib.sha256(payload).hexdigest()
    calls: list[str] = []

    def opener(request, *, timeout):
        calls.append(request.full_url)
        if "official" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        return _Response(payload)

    destination = tmp_path / "runtime.zip"
    source = mod._download(
        ["https://official.invalid/runtime.zip", "https://mirror.invalid/runtime.zip"],
        destination,
        label="runtime",
        expected_sha256=expected,
        opener=opener,
        sleeper=lambda _delay: None,
    )

    assert source == "https://mirror.invalid/runtime.zip"
    assert calls == [
        "https://official.invalid/runtime.zip",
        "https://mirror.invalid/runtime.zip",
    ]


def test_download_checksum_mismatch_is_fatal_and_leaves_no_partial_file(tmp_path: Path) -> None:
    mod = _load_module()
    calls: list[str] = []

    def opener(request, *, timeout):
        calls.append(request.full_url)
        return _Response(b"tampered archive")

    destination = tmp_path / "runtime.zip"
    with pytest.raises(mod.ArchiveChecksumMismatch):
        mod._download(
            ["https://official.invalid/runtime.zip", "https://mirror.invalid/runtime.zip"],
            destination,
            label="runtime",
            expected_sha256=hashlib.sha256(b"expected archive").hexdigest(),
            opener=opener,
            sleeper=lambda _delay: None,
        )

    assert calls == ["https://official.invalid/runtime.zip"]
    assert not destination.exists()
    assert not destination.with_name("runtime.zip.part").exists()


def test_replace_reuses_only_a_verified_cached_archive(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    vendor = tmp_path / "vendor"
    archive = vendor / "_downloads" / "runtime.zip"
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("runtime/innoextract.exe", b"binary")
    expected = mod._sha256(archive)
    monkeypatch.setattr(mod, "VENDOR", vendor)
    monkeypatch.setattr(
        mod,
        "_download",
        lambda *_args, **_kwargs: pytest.fail("verified cache should not download"),
    )

    destination = vendor / "innoextract" / "win-x64"
    mod._replace_from_archive(
        urls=["https://official.invalid/runtime.zip"],
        filename="runtime.zip",
        expected_sha256=expected,
        destination=destination,
        label="runtime",
        force=False,
    )

    assert (destination / "innoextract.exe").read_bytes() == b"binary"


def test_replace_discards_corrupt_cache_before_download(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    vendor = tmp_path / "vendor"
    archive = vendor / "_downloads" / "runtime.zip"
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"corrupt cache")
    clean = tmp_path / "clean.zip"
    with zipfile.ZipFile(clean, "w") as bundle:
        bundle.writestr("runtime/innoextract.exe", b"binary")
    expected = mod._sha256(clean)
    calls: list[bool] = []

    def download(_urls, destination, **_kwargs):
        calls.append(not destination.exists())
        destination.write_bytes(clean.read_bytes())
        return "https://official.invalid/runtime.zip"

    monkeypatch.setattr(mod, "VENDOR", vendor)
    monkeypatch.setattr(mod, "_download", download)
    destination = vendor / "innoextract" / "win-x64"
    mod._replace_from_archive(
        urls=["https://official.invalid/runtime.zip"],
        filename="runtime.zip",
        expected_sha256=expected,
        destination=destination,
        label="runtime",
        force=False,
    )

    assert calls == [True]
    assert (destination / "innoextract.exe").read_bytes() == b"binary"
