from __future__ import annotations

from argusai.core.assistant.session_blocks import SessionBlocksMixin


def test_evidence_add_renders_structured_case_block() -> None:
    blocks = SessionBlocksMixin()._blocks_for_tool_trace([{
        "name": "evidence_add",
        "arguments": {"case_id": "case-20260731-Ab12"},
        "result": {
            "ok": True,
            "case_id": "case-20260731-Ab12",
            "evidence_ref": "ev-Ab12cd",
            "evidence": {
                "id": "ev-Ab12cd",
                "kind": "ai",
                "title": "Tool call escaped policy",
                "confidence": "confirmed",
                "summary": "Observed an unauthorized target-side tool call.",
                "artifact_refs": ["art-Ab12cd"],
            },
        },
    }])

    assert blocks == [{
        "type": "case_evidence",
        "title": "Tool call escaped policy",
        "ok": True,
        "error": "",
        "record_type": "evidence",
        "case_id": "case-20260731-Ab12",
        "ref_id": "ev-Ab12cd",
        "kind": "ai",
        "confidence": "confirmed",
        "summary": "Observed an unauthorized target-side tool call.",
        "path": "",
        "artifact_refs": ["art-Ab12cd"],
    }]


def test_artifact_add_renders_structured_case_block() -> None:
    block = SessionBlocksMixin()._blocks_for_tool_trace([{
        "name": "artifact_add",
        "result": {
            "ok": True,
            "case_id": "case-20260731-Ab12",
            "artifact_ref": "art-Ab12cd",
            "artifact": {
                "id": "art-Ab12cd",
                "kind": "sbom",
                "title": "CycloneDX inventory",
                "path": "artifacts/sbom.json",
            },
        },
    }])[0]

    assert block["type"] == "case_evidence"
    assert block["record_type"] == "artifact"
    assert block["kind"] == "sbom"
    assert block["ref_id"] == "art-Ab12cd"
