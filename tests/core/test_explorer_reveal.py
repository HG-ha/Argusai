"""Windows file-manager reveal must not hide Explorer behind quiet kwargs."""

from __future__ import annotations

from pathlib import Path

from argusai.core.infra import explorer


def test_reveal_path_directory_uses_startfile_on_windows(monkeypatch, tmp_path: Path):
    calls: list[str] = []

    monkeypatch.setattr(explorer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(explorer.os, "startfile", lambda path: calls.append(str(path)))
    monkeypatch.setattr(
        explorer.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Popen should not open dirs")),
    )

    folder = tmp_path / "sandbox"
    folder.mkdir()
    result = explorer.reveal_path(folder)

    assert result["ok"] is True
    assert calls == [str(folder.resolve())]


def test_reveal_path_file_uses_explorer_select_without_hide_flags(monkeypatch, tmp_path: Path):
    pops: list[tuple[list[str], dict]] = []

    monkeypatch.setattr(explorer.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        explorer.subprocess,
        "Popen",
        lambda args, **kwargs: pops.append((list(args), dict(kwargs))),
    )

    file_path = tmp_path / "notes.txt"
    file_path.write_text("hi", encoding="utf-8")
    result = explorer.reveal_path(file_path)

    assert result["ok"] is True
    assert len(pops) == 1
    args, kwargs = pops[0]
    assert args[0] == "explorer"
    assert args[1] == "/select,"
    assert Path(args[2]) == file_path.resolve()
    # Quiet/hide STARTUPINFO would make Explorer appear to do nothing.
    assert "startupinfo" not in kwargs
    assert kwargs.get("creationflags", 0) == 0


def test_reveal_in_folder_delegates_to_reveal_path(monkeypatch, tmp_path: Path):
    from argusai.core.workspace import session_sandbox

    seen: list[Path] = []
    monkeypatch.setattr(
        "argusai.core.infra.explorer.reveal_path",
        lambda target: seen.append(Path(target)) or {"ok": True, "path": str(target)},
    )

    root = tmp_path / "root"
    root.mkdir()
    nested = root / "mockloc-project"
    nested.mkdir()
    out = session_sandbox.reveal_in_folder(root, "mockloc-project")
    assert out["ok"] is True
    assert seen and seen[-1] == nested.resolve()
