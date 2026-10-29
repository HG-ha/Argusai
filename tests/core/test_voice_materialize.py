import base64
from pathlib import Path

from argusai.core.assistant.attachments import _materialize_voice


def test_materialize_voice_spills_to_sandbox_and_strips_inline_data(tmp_path: Path) -> None:
    payload = {
        "type": "audio",
        "mime": "audio/webm",
        "duration_ms": 1200,
        "data_base64": base64.b64encode(b"fake-webm-bytes").decode("ascii"),
    }
    out = _materialize_voice(payload, tmp_path)
    assert out is not None
    assert "data_base64" not in out
    rel = out.get("sandbox_rel") or ""
    assert rel.startswith("_voice/")
    target = tmp_path / rel
    assert target.is_file()
    assert target.read_bytes() == b"fake-webm-bytes"


def test_materialize_voice_keeps_inline_when_no_sandbox() -> None:
    payload = {
        "type": "audio",
        "mime": "audio/webm",
        "data_base64": base64.b64encode(b"x").decode("ascii"),
    }
    out = _materialize_voice(payload, None)
    assert out is not None
    assert out.get("data_base64")
