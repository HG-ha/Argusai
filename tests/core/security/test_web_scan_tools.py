"""Tests for nuclei/ffuf web scan bridges."""

from __future__ import annotations

import json

from argusai.core.security.web_scan_tools import (
    ffuf_plan_scan,
    nuclei_plan_scan,
    parse_ffuf_json,
    parse_nuclei_jsonl,
    web_scan_tool_inventory,
)


def test_web_scan_tool_inventory_has_bundled_wordlist() -> None:
    inv = web_scan_tool_inventory()
    assert inv["ok"] is True
    assert inv["bundled_wordlists"]["entry_count"] > 0


def test_nuclei_plan_scan_blocks_out_of_scope() -> None:
    plan = nuclei_plan_scan("https://evil.example/api", scope="allowed.example")
    assert plan["ok"] is False
    assert "scope" in plan["error"]


def test_nuclei_plan_scan_builds_safe_argv(monkeypatch) -> None:
    monkeypatch.setattr(
        "argusai.core.security.web_scan_tools.nuclei_status",
        lambda: {"installed": True, "path": "/usr/bin/nuclei"},
    )
    plan = nuclei_plan_scan("https://allowed.example/", scope="allowed.example", tags="cve")
    assert plan["ok"] is True
    assert plan["args"][:2] == ["-u", "https://allowed.example/"]
    assert "-no-interactsh" in plan["args"]
    assert "-tags" in plan["args"]


def test_nuclei_plan_scan_blocks_update_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        "argusai.core.security.web_scan_tools.nuclei_status",
        lambda: {"installed": True, "path": "/usr/bin/nuclei"},
    )
    plan = nuclei_plan_scan("https://allowed.example/", extra_args=["-update"])
    assert plan["ok"] is False


def test_parse_nuclei_jsonl_extracts_findings() -> None:
    line = json.dumps({
        "template-id": "tech-detect",
        "info": {"name": "Tech Detect", "severity": "info"},
        "host": "https://example.com",
        "matched-at": "https://example.com/",
        "type": "http",
    })
    parsed = parse_nuclei_jsonl(line + "\n")
    assert parsed["finding_count"] == 1
    assert parsed["findings"][0]["template_id"] == "tech-detect"


def test_ffuf_plan_scan_appends_fuzz(monkeypatch) -> None:
    monkeypatch.setattr(
        "argusai.core.security.web_scan_tools.ffuf_status",
        lambda: {"installed": True, "path": "/usr/bin/ffuf"},
    )
    plan = ffuf_plan_scan("https://allowed.example", scope="allowed.example")
    assert plan["ok"] is True
    assert plan["target"].endswith("/FUZZ")
    assert "-w" in plan["args"]


def test_parse_ffuf_json_reads_results() -> None:
    payload = {
        "results": [
            {"url": "https://example.com/admin", "status": 200, "length": 100, "input": {"FUZZ": "admin"}},
        ]
    }
    parsed = parse_ffuf_json(json.dumps(payload))
    assert parsed["result_count"] == 1
    assert parsed["results"][0]["input"] == "admin"
