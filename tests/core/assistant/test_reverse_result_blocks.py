"""reverse_result assistant cards for mobile/native tools."""

from __future__ import annotations

from argusai.core.assistant.session_blocks import _reverse_result_block


def test_decompile_block_includes_output_dir() -> None:
    block = _reverse_result_block(
        "android_apktool_decode",
        {
            "ok": True,
            "partial": True,
            "output_dir": r"C:\jobs\rev-1\decompiled\apktool-abc",
            "tree": {"file_count": 3300},
            "attached": {
                "job_id": "rev-1",
                "index_artifact_ref": "rev://rev-1/art-abc",
                "output_dir": r"C:\jobs\rev-1\decompiled\apktool-abc",
            },
        },
    )
    assert block["type"] == "reverse_result"
    assert block["output_dir"].endswith("apktool-abc")
    assert block["file_count"] == 3300
    assert block["partial"] is True
    assert "reverse_artifact_grep" in block["next_step"]
