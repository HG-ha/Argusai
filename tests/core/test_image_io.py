"""Raster image reads for assistant tools — host/project paths, not sandbox-only."""

from __future__ import annotations

from pathlib import Path

from argusai.core.assistant.image_io import (
    image_read_tool_result,
    is_raster_image_path,
    load_image_file,
    resolve_inspectable_image,
)
from argusai.core.tools.sandbox_code import read_text_file, sandbox_read_file

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc``\x00\x00"
    b"\x00\x04\x00\x01\xa3\n\x15\xe3\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_is_raster_image_path_accepts_common_stills() -> None:
    assert is_raster_image_path("C:/shots/a.png")
    assert is_raster_image_path("photo.JPEG")
    assert is_raster_image_path(Path("x.webp"))
    assert not is_raster_image_path("icon.svg")
    assert not is_raster_image_path("notes.txt")


def test_resolve_inspectable_image_prefers_absolute_host_path(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    host = tmp_path / "host.png"
    host.write_bytes(TINY_PNG)
    (sandbox / "host.png").write_bytes(b"not-the-host")
    found = resolve_inspectable_image(str(host), search_roots=[sandbox])
    assert found == host.resolve()


def test_resolve_inspectable_image_tries_search_roots(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    target = project / "ui.png"
    target.write_bytes(TINY_PNG)
    found = resolve_inspectable_image("ui.png", search_roots=[tmp_path / "missing", project])
    assert found == target.resolve()


def test_load_image_file_and_attach_payload(tmp_path: Path) -> None:
    path = tmp_path / "a.png"
    path.write_bytes(TINY_PNG)
    loaded = load_image_file(path)
    assert loaded["ok"] is True
    assert loaded["mime"] == "image/png"
    payload = image_read_tool_result(path)
    assert payload["kind"] == "image"
    assert payload["attached_to_model"] is True
    assert payload["_assistant_attachments"][0]["data_base64"]


def test_read_text_file_attaches_host_image(tmp_path: Path) -> None:
    path = tmp_path / "desk.png"
    path.write_bytes(TINY_PNG)
    out = read_text_file(api=None, path=str(path))
    assert out["ok"] is True
    assert out["kind"] == "image"
    assert out["attached_to_model"] is True
    assert "sandbox" in out["hint"].lower()


def test_sandbox_read_file_attaches_sandbox_image(tmp_path: Path, monkeypatch) -> None:
    img = tmp_path / "shot.png"
    img.write_bytes(TINY_PNG)
    monkeypatch.setattr(
        "argusai.core.tools.sandbox_code._session_sandbox_root",
        lambda api, kwargs: tmp_path,
    )
    out = sandbox_read_file(api=None, path="shot.png")
    assert out["ok"] is True
    assert out["kind"] == "image"
    assert out["relative_path"] == "shot.png"
    assert out["_assistant_attachments"]
