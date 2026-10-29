"""Integration tests for binary_capa_analyze / binary_yara_scan agent tools."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from argusai.core.reverse.reverse_jobs import ReverseJobStore
from argusai.core.tools.native_reverse import binary_capa_analyze, binary_yara_scan


@patch("argusai.core.tools.native_reverse._preflight_cli_tool", return_value=None)
@patch("argusai.core.tools.native_reverse.run_capa_analyze")
def test_binary_capa_analyze_attaches_report(mock_run: MagicMock, _mock_preflight: MagicMock, tmp_path: Path) -> None:
    store = ReverseJobStore(tmp_path / "reverse_jobs")
    sample = tmp_path / "sample.exe"
    sample.write_bytes(b"MZ")
    job = store.create(title="capa test", target_type="windows", platform="windows")
    api = type("Api", (), {"reverse_jobs": store})()
    mock_run.return_value = {
        "ok": True,
        "rule_count": 2,
        "report": {"meta": {"format": "pe"}, "rules": {"a": {}, "b": {}}},
        "finished_at": "2026-05-23T12:00:00",
    }

    out = binary_capa_analyze(api, path=str(sample), job_id=job["id"])
    assert out["ok"] is True
    assert out["attached"]["artifact"]["kind"] == "capa_report"
    arts = store.get(job["id"])["artifacts"]
    assert len(arts) == 1
    payload = store.read_artifact(job["id"], arts[0]["id"], max_bytes=65536)
    assert payload is not None
    data = json.loads(payload["content"])
    assert len(data.get("rules") or data) >= 1 or "rules" in data


@patch("argusai.core.tools.native_reverse._preflight_cli_tool", return_value=None)
@patch("argusai.core.tools.native_reverse.run_yara_scan")
def test_binary_yara_scan_attaches_matches(mock_run: MagicMock, _mock_preflight: MagicMock, tmp_path: Path) -> None:
    store = ReverseJobStore(tmp_path / "reverse_jobs")
    sample = tmp_path / "sample.exe"
    sample.write_bytes(b"MZ")
    rules = tmp_path / "rules.yar"
    rules.write_text('rule x { strings: $a = "MZ" condition: $a at 0 }', encoding="utf-8")
    job = store.create(title="yara test", target_type="windows", platform="windows")
    api = type("Api", (), {"reverse_jobs": store})()
    mock_run.return_value = {
        "ok": True,
        "match_count": 1,
        "match_lines": ["x sample.exe"],
        "rules": str(rules),
        "input": str(sample),
        "finished_at": "2026-05-23T12:00:00",
    }

    out = binary_yara_scan(api, path=str(sample), rules_path=str(rules), job_id=job["id"])
    assert out["ok"] is True
    assert out["attached"]["artifact"]["kind"] == "yara_match"
    arts = store.get(job["id"])["artifacts"]
    assert arts[0]["tool"] == "binary_yara_scan"


@patch("argusai.core.tools.native_reverse._preflight_cli_tool", return_value=None)
def test_binary_yara_scan_requires_rules_path(_mock_preflight: MagicMock, tmp_path: Path) -> None:
    api = type("Api", (), {"reverse_jobs": None})()
    out = binary_yara_scan(api, path=str(tmp_path / "x.exe"), job_id="")
    assert out["ok"] is False
    assert "rules_path" in out["error"]
