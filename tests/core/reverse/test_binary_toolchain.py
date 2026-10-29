"""Tests for capa / yara CLI bridge."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from argusai.core.reverse import binary_toolchain


@patch("argusai.core.reverse.binary_toolchain._tool_executable", return_value=(None, {"available": False}))
def test_run_capa_missing_tool(_mock: MagicMock) -> None:
    out = binary_toolchain.run_capa_analyze(Path("sample.exe"))
    assert out["ok"] is False
    assert "capa not installed" in out["error"]


@patch("argusai.core.reverse.binary_toolchain._tool_executable", return_value=("/bin/capa", {"available": True}))
@patch("argusai.core.reverse.binary_toolchain._run_cli")
def test_run_capa_success(mock_run: MagicMock, _mock_tool: MagicMock, tmp_path: Path) -> None:
    sample = tmp_path / "sample.exe"
    sample.write_bytes(b"MZ")
    report = {"meta": {"format": "pe"}, "rules": {"rule.a": {}, "rule.b": {}}}

    def _write_json(cmd: list[str], *, timeout_seconds: int) -> dict[str, object]:
        out_idx = cmd.index("-o") + 1
        Path(cmd[out_idx]).write_text(json.dumps(report), encoding="utf-8")
        return {"ok": True, "command": "capa", "returncode": 0, "stdout": "", "stderr": "", "error": ""}

    mock_run.side_effect = _write_json
    result = binary_toolchain.run_capa_analyze(sample, format_hint="pe", timeout_seconds=30)
    assert result["ok"] is True
    assert result["rule_count"] == 2
    assert mock_run.call_args[0][0][:3] == ["/bin/capa", str(sample.resolve()), "-j"]


@patch("argusai.core.reverse.binary_toolchain._tool_executable", return_value=(None, {"available": False}))
def test_run_yara_missing_tool(_mock: MagicMock, tmp_path: Path) -> None:
    sample = tmp_path / "sample.exe"
    sample.write_bytes(b"MZ")
    rules = tmp_path / "rules.yar"
    rules.write_text('rule x { strings: $a = "MZ" condition: $a at 0 }', encoding="utf-8")
    out = binary_toolchain.run_yara_scan(sample, rules)
    assert out["ok"] is False
    assert "yara not installed" in out["error"]


@patch("argusai.core.reverse.binary_toolchain._tool_executable", return_value=("/bin/yara", {"available": True}))
@patch("argusai.core.reverse.binary_toolchain._run_cli")
def test_run_yara_success(mock_run: MagicMock, _mock_tool: MagicMock, tmp_path: Path) -> None:
    sample = tmp_path / "sample.exe"
    sample.write_bytes(b"MZ")
    rules = tmp_path / "rules.yar"
    rules.write_text('rule x { strings: $a = "MZ" condition: $a at 0 }', encoding="utf-8")
    mock_run.return_value = {
        "ok": True,
        "command": "yara",
        "returncode": 0,
        "stdout": "x sample.exe\n",
        "stderr": "",
        "error": "",
    }
    result = binary_toolchain.run_yara_scan(sample, rules, timeout_seconds=15)
    assert result["ok"] is True
    assert result["match_count"] == 1
    assert mock_run.call_args[0][0] == ["/bin/yara", "-s", str(rules.resolve()), str(sample.resolve())]
