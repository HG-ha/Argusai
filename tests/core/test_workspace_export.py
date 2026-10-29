from pathlib import Path

from argusai.core.workspace.workspace_export import (
    copy_sandbox_tree,
    copy_selected_sandbox_paths,
    export_task_report,
)


class _TaskStub:
    def __init__(self, root: Path) -> None:
        self.root = root


def test_auto_export_includes_verification_snippets(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    tmp = sandbox / ".tmp"
    tmp.mkdir(parents=True)
    (tmp / "snippet-1700000000-abcd1234.py").write_text("print('verified')\n", encoding="utf-8")
    (tmp / "scratch.txt").write_text("internal scratch\n", encoding="utf-8")
    (sandbox / "audit.jsonl").write_text("{}\n", encoding="utf-8")

    out = tmp_path / "out"
    copied = copy_sandbox_tree(sandbox, out, selection_mode="auto")

    paths = {Path(item["path"]).relative_to(out).as_posix(): item for item in copied}
    assert "verification-scripts/snippet-1700000000-abcd1234.py" in paths
    assert paths["verification-scripts/snippet-1700000000-abcd1234.py"]["selection_reason"] == "verification snippet script"
    assert ".tmp/scratch.txt" not in paths
    assert "audit.jsonl" not in paths


def test_auto_export_keeps_regular_deliverable_scripts(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "verify_payment.py").write_text("print('ok')\n", encoding="utf-8")

    out = tmp_path / "out"
    copied = copy_sandbox_tree(sandbox, out, selection_mode="auto")

    assert [Path(item["path"]).relative_to(out).as_posix() for item in copied] == ["verify_payment.py"]


def test_ai_selected_paths_control_export_contents(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    evidence = sandbox / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "raw.bin").write_bytes(b"\x00\x01")
    (evidence / "notes.tmp").write_text("kept because directory was explicit\n", encoding="utf-8")
    (sandbox / "unused_report.md").write_text("not selected\n", encoding="utf-8")
    (sandbox / "audit.jsonl").write_text("{}\n", encoding="utf-8")

    out = tmp_path / "out"
    copied = copy_selected_sandbox_paths(sandbox, out, ["evidence"], max_files=20)

    paths = {Path(item["path"]).relative_to(out).as_posix(): item for item in copied}
    assert paths.keys() == {"evidence/raw.bin", "evidence/notes.tmp"}
    assert all(item["selection_reason"] == "explicitly selected by AI" for item in paths.values())


def test_ai_selected_paths_cannot_escape_sandbox(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")

    copied = copy_selected_sandbox_paths(sandbox, tmp_path / "out", [str(outside)])

    assert copied == []


def test_report_export_defaults_to_customer_handoff(tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    task_root.mkdir()
    for name in ("report.html", "report.md", "report.json", "findings.jsonl"):
        (task_root / name).write_text(name, encoding="utf-8")

    result = export_task_report(_TaskStub(task_root), tmp_path / "out")

    copied_names = {Path(item["path"]).name for item in result["copied"]}
    assert copied_names == {"report.html", "report.md"}
    assert result["profile"] == "customer"
    assert result["omitted_internal_formats"] == ["json", "findings"]
    assert not (tmp_path / "out" / "report.json").exists()
    assert not (tmp_path / "out" / "findings.jsonl").exists()


def test_report_export_allows_explicit_internal_formats(tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    task_root.mkdir()
    for name in ("report.html", "report.md", "report.json", "findings.jsonl"):
        (task_root / name).write_text(name, encoding="utf-8")

    result = export_task_report(
        _TaskStub(task_root),
        tmp_path / "out",
        formats=["html", "md", "json", "findings"],
    )

    copied_names = {Path(item["path"]).name for item in result["copied"]}
    assert copied_names == {"report.html", "report.md", "report.json", "findings.jsonl"}
    assert result["profile"] == "custom_fmts"
    assert "omitted_internal_formats" not in result


def test_report_export_warns_about_existing_internal_files(tmp_path: Path) -> None:
    task_root = tmp_path / "task"
    task_root.mkdir()
    (task_root / "report.html").write_text("html", encoding="utf-8")
    (task_root / "report.md").write_text("md", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "findings.jsonl").write_text("old internal\n", encoding="utf-8")

    result = export_task_report(_TaskStub(task_root), out)

    assert result["warnings"]
    assert "findings.jsonl" in result["warnings"][0]
    assert (out / "findings.jsonl").read_text(encoding="utf-8") == "old internal\n"
