"""Host-path attachments stay on disk; tools read them in place."""

from __future__ import annotations

import base64
from pathlib import Path

from argusai.core.agent_runtimes.acp_turn import _resource_uri, build_acp_prompt_blocks
from argusai.core.assistant.attachments import (
    _materialize_acp_media_attachments,
    _materialize_binary_attachments,
    _materialize_text_attachments,
    _sanitize_attachments,
)
from argusai.core.assistant.host_paths import normalize_host_path


def test_normalize_host_path_requires_absolute(tmp_path: Path) -> None:
    host = tmp_path / "notes.txt"
    host.write_text("hello", encoding="utf-8")
    assert normalize_host_path(str(host)) == str(host.resolve())
    assert normalize_host_path("notes.txt") == ""
    assert normalize_host_path("") == ""


def test_sanitize_keeps_path_only_file(tmp_path: Path) -> None:
    host = tmp_path / "report.pdf"
    host.write_bytes(b"%PDF")
    out = _sanitize_attachments([{
        "type": "file",
        "name": "report.pdf",
        "host_path": str(host),
        "size": 4,
    }])
    assert len(out) == 1
    assert out[0]["host_path"] == str(host.resolve())
    assert "data_base64" not in out[0]


def test_materialize_path_ref_does_not_copy(tmp_path: Path) -> None:
    host = tmp_path / "source.py"
    host.write_text("print(1)\n", encoding="utf-8")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    stored, note = _materialize_binary_attachments(
        [{
            "type": "file",
            "name": "source.py",
            "host_path": str(host),
            "size": host.stat().st_size,
        }],
        sandbox,
    )
    assert stored[0]["host_path"] == str(host.resolve())
    assert "sandbox_path" not in stored[0]
    assert not any(path.is_file() for path in sandbox.rglob("*"))
    assert str(host.resolve()) in note
    assert "sandbox copy skipped" in note or "original path" in note


def test_materialize_text_with_host_path_skips_sandbox(tmp_path: Path) -> None:
    host = tmp_path / "readme.md"
    host.write_text("# hi\n", encoding="utf-8")
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    stored, note = _materialize_text_attachments(
        [{
            "type": "text",
            "name": "readme.md",
            "mime": "text/markdown",
            "host_path": str(host),
            "text": "# hi\n",
            "size": 5,
        }],
        sandbox,
    )
    assert stored[0]["host_path"] == str(host.resolve())
    assert "sandbox_path" not in stored[0]
    assert not (sandbox / "_attachments").exists() or list((sandbox / "_attachments").iterdir()) == []
    assert str(host.resolve()) in note


def test_acp_resource_uri_prefers_host_path(tmp_path: Path) -> None:
    host = tmp_path / "shot.png"
    host.write_bytes(b"png")
    uri = _resource_uri({
        "host_path": str(host),
        "sandbox_path": str(tmp_path / "sandbox" / "copied.png"),
    })
    assert uri.startswith("file:")
    assert host.name in uri


def test_acp_path_only_image_becomes_resource_link(tmp_path: Path) -> None:
    host = tmp_path / "shot.png"
    host.write_bytes(b"png")
    raw = [{
        "type": "image",
        "mime": "image/png",
        "name": "shot.png",
        "host_path": str(host),
    }]
    stored = _materialize_acp_media_attachments(raw, tmp_path / "sandbox")
    assert stored[0]["host_path"] == str(host.resolve())
    assert "sandbox_path" not in stored[0]
    blocks = build_acp_prompt_blocks("look", raw, stored, {
        "image": False,
        "audio": False,
        "embedded_context": True,
    })
    payloads = [
        block.model_dump(mode="json", by_alias=True, exclude_none=True)
        for block in blocks
    ]
    assert payloads[1]["type"] == "resource_link"
    assert payloads[1]["uri"].startswith("file:")


def test_bytes_without_host_path_still_spill_to_sandbox(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    payload = base64.b64encode(b"MZpayload").decode()
    stored, _note = _materialize_binary_attachments(
        [{
            "type": "file",
            "name": "sample.bin",
            "data_base64": payload,
        }],
        sandbox,
    )
    assert stored[0]["relative_path"].startswith("_attachments/")
    assert (sandbox / stored[0]["relative_path"]).read_bytes() == b"MZpayload"
