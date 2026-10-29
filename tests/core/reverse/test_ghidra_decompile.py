"""Ghidra headless native-analysis backend and generic binary tool wiring."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import argusai.core.reverse.ghidra_runtime as gr
import argusai.core.tools.native_reverse as nr


@pytest.fixture()
def sample(tmp_path: Path) -> Path:
    path = tmp_path / "a.exe"
    path.write_bytes(b"MZ" + b"\x00" * 64)
    return path


def test_resolve_via_ghidra_install_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from argusai.core.reverse.toolchain import clear_toolchain_cache

    support = tmp_path / "support"
    support.mkdir()
    headless = support / ("analyzeHeadless.bat" if sys.platform == "win32" else "analyzeHeadless")
    headless.write_text("", encoding="utf-8")
    clear_toolchain_cache()
    monkeypatch.setenv("GHIDRA_INSTALL_DIR", str(tmp_path))

    path, _info = gr.resolve_analyze_headless(None)

    assert path == str(headless.resolve())
    assert gr.ghidra_available(None) is True


def _fake_run_writes(result: dict) -> object:
    def _run(argv, *, timeout_seconds):
        out = Path(argv[-1])
        out.write_text(json.dumps(result), encoding="utf-8")
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    return _run


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: dict,
) -> None:
    monkeypatch.setattr(
        gr,
        "resolve_analyze_headless",
        lambda api=None: ("/opt/ghidra/support/analyzeHeadless", {"available": True}),
    )
    monkeypatch.setattr(gr, "_project_dir", lambda api=None: tmp_path / "projects")
    monkeypatch.setattr(gr, "_run_cli", _fake_run_writes(payload))


def test_decompile_function_success(
    sample: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(
        monkeypatch,
        tmp_path,
        {"ok": True, "c": "int main(void) { return 0; }", "function": "main", "entry": "0x401000"},
    )

    out = gr.decompile_function(None, sample, function="main")

    assert out["ok"] is True
    assert out["engine"] == "ghidra"
    assert "int main" in out["pseudocode"]
    assert out["function"] == "main"


def test_decompile_function_reports_error(
    sample: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(monkeypatch, tmp_path, {"ok": False, "error": "function not found: nope"})

    out = gr.decompile_function(None, sample, function="nope")

    assert out["ok"] is False
    assert "not found" in out["error"]


def test_decompile_function_unavailable(sample: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gr, "resolve_analyze_headless", lambda api=None: (None, {"available": False}))

    out = gr.decompile_function(None, sample, function="main")

    assert out["ok"] is False
    assert out.get("toolchain_required") == "ghidra"


def test_list_functions_returns_bounded_normalized_result(
    sample: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(
        monkeypatch,
        tmp_path,
        {
            "ok": True,
            "function_count": 2,
            "functions": [
                {"name": "big", "offset": 0x401000, "vaddr": 0x401000, "size": 128},
                {"name": "small", "offset": 0x402000, "vaddr": 0x402000, "size": 16},
            ],
            "truncated": False,
        },
    )

    out = gr.list_functions(None, sample, limit=10)

    assert out["ok"] is True
    assert out["tool"] == "ghidra"
    assert out["function_count"] == 2
    assert out["functions"][0]["name"] == "big"
    assert out["truncated"] is False


def test_disassemble_function_returns_text(
    sample: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runtime(
        monkeypatch,
        tmp_path,
        {
            "ok": True,
            "function": "main",
            "entry": "00401000",
            "mode": "function",
            "instruction_count": 2,
            "disasm": "00401000  PUSH RBP\n00401001  MOV RBP,RSP",
        },
    )

    out = gr.disassemble_function(None, sample, function="main")

    assert out["ok"] is True
    assert out["engine"] == "ghidra"
    assert out["instruction_count"] == 2
    assert "PUSH RBP" in out["disasm"]


def _patch_native_common(monkeypatch: pytest.MonkeyPatch, sample: Path) -> None:
    monkeypatch.setattr(nr, "_resolve_binary_sample", lambda *a, **k: sample)
    monkeypatch.setattr(nr, "_resolve_reverse_store", lambda api: None)


def test_generic_binary_tools_delegate_to_ghidra(
    sample: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_native_common(monkeypatch, sample)
    monkeypatch.setattr(
        nr,
        "run_ghidra_list_functions",
        lambda api, path, **kwargs: {"ok": True, "tool": "ghidra", "functions": []},
    )
    monkeypatch.setattr(
        nr,
        "run_ghidra_disassemble",
        lambda api, path, **kwargs: {"ok": True, "tool": "ghidra", "disasm": "RET", "target": "main"},
    )
    monkeypatch.setattr(
        nr,
        "run_ghidra_decompile",
        lambda api, path, **kwargs: {"ok": True, "engine": "ghidra", "pseudocode": "return 0;", "target": "main"},
    )

    assert nr.binary_list_functions(api=object(), path=str(sample))["tool"] == "ghidra"
    assert nr.binary_disassemble(api=object(), path=str(sample), function="main")["disasm"] == "RET"
    assert nr.binary_decompile(api=object(), path=str(sample), function="main")["engine"] == "ghidra"


def test_binary_decompile_schema_has_no_backend_selector() -> None:
    from argusai.core.assistant.skills import EXPLICIT_SCHEMAS

    assert "engine" not in EXPLICIT_SCHEMAS["binary_decompile"]
