from __future__ import annotations

from argusai.core.assistant.session_blocks import SessionBlocksMixin


def test_reverse_job_create_renders_reverse_result_with_job_route_id() -> None:
    block = SessionBlocksMixin()._blocks_for_tool_trace([{
        "name": "reverse_job_create",
        "arguments": {"title": "Signed request analysis"},
        "result": {
            "ok": True,
            "job": {
                "id": "rev-20260803-Ab12",
                "title": "Signed request analysis",
                "status": "active",
            },
        },
    }])[0]

    assert block["type"] == "reverse_result"
    assert block["job_id"] == "rev-20260803-Ab12"
    assert "Created ReverseJob rev-20260803-Ab12" in block["summary"]
