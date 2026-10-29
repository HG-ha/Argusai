"""Tests for explicit data_root copy / move / switch migration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock

from argusai.core.infra.logging_setup import LoggingSettings
from argusai.core.paths import PERSISTENT_DATA_SUBDIRS, PathManager
from argusai.core.workspace.data_root_migrate import (
    apply_data_root_change,
    finalize_data_root_move,
    preview_data_root_change,
    rebase_path,
    repair_mcp_venv_launchers,
)


@dataclass
class _FakeSettings:
    data_root: str
    toolchain_paths: dict[str, str] = field(default_factory=dict)
    mcp_servers: list[dict] = field(default_factory=list)
    logging: LoggingSettings = field(default_factory=LoggingSettings)


def _write_tree(root: Path) -> None:
    (root / "tasks" / "t1").mkdir(parents=True)
    (root / "tasks" / "t1" / "task.json").write_text('{"id":"t1"}', encoding="utf-8")
    (root / "knowledge").mkdir(parents=True)
    (root / "knowledge" / "note.md").write_text("hello", encoding="utf-8")
    (root / "assistant_sessions").mkdir(parents=True)
    (root / "assistant_sessions" / "s1.json").write_text("{}", encoding="utf-8")
    for name in ("cases", "session_vault", "user_todos", "assistant_cron", "channels"):
        (root / name).mkdir(parents=True)
        (root / name / "data.json").write_text("{}", encoding="utf-8")


def test_path_manager_uses_canonical_persistent_subdirs(tmp_path: Path) -> None:
    expected = {
        "tasks",
        "assistant_sessions",
        "poc_pages",
        "knowledge",
        "reverse_jobs",
        "tools",
        "proxy",
        "cases",
        "session_vault",
        "user_todos",
        "assistant_cron",
        "channels",
    }
    assert set(PERSISTENT_DATA_SUBDIRS) == expected
    manager = PathManager(app_root=tmp_path / "app", data_root=tmp_path / "data")
    manager.ensure_base_dirs()
    assert all((tmp_path / "data" / name).is_dir() for name in expected)
    assert manager.cases_dir.name == "cases"
    assert manager.session_vault_dir.name == "session_vault"
    assert manager.user_todos_dir.name == "user_todos"
    assert manager.assistant_cron_dir.name == "assistant_cron"
    assert manager.channels_dir.name == "channels"


def test_rebase_path_under_old_root(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    src = old / "tools" / "reverse" / "jadx.bat"
    assert rebase_path(str(src), old, new) == str(new / "tools" / "reverse" / "jadx.bat")
    assert rebase_path("relative/path", old, new) is None


def test_repair_moved_mcp_windows_venv_launcher(tmp_path: Path) -> None:
    venv = tmp_path / "mcp_stage" / "venv"
    scripts = venv / "Scripts"
    site = venv / "Lib" / "site-packages" / "demo_mcp-1.0.dist-info"
    scripts.mkdir(parents=True)
    site.mkdir(parents=True)
    (scripts / "python.exe").write_bytes(b"python")
    launcher = scripts / "demo-mcp.exe"
    launcher.write_bytes(b"launcher-with-old-absolute-path")
    (site / "entry_points.txt").write_text(
        "[console_scripts]\ndemo-mcp = demo_mcp.cli:main\n",
        encoding="utf-8",
    )
    settings = _FakeSettings(
        data_root=str(tmp_path),
        mcp_servers=[{
            "id": "demo",
            "name": "demo",
            "command": str(launcher),
            "args": ["stdio"],
        }],
    )

    repairs, updated = repair_mcp_venv_launchers(settings)

    server = updated.mcp_servers[0]
    assert len(repairs) == 1
    assert server["command"] == str((scripts / "python.exe").resolve())
    assert server["args"][-1] == "stdio"
    assert "demo_mcp.cli" in server["args"][1]


def test_preview_blocks_nested_destination(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    preview = preview_data_root_change(old, old / "nested")
    assert preview["ok"] is False
    assert any(b["code"] == "dest_inside_source" for b in preview["blockers"])


def test_copy_migrates_managed_subdirs_and_rewrites_paths(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_tree(old)
    tool = old / "tools" / "reverse" / "jadx.bat"
    tool.parent.mkdir(parents=True)
    tool.write_text("x", encoding="utf-8")
    command = old / "assistant_sessions" / "x.sandbox" / "bin.exe"
    command.parent.mkdir(parents=True)
    command.write_text("exe", encoding="utf-8")
    (old / "tools" / "cfg.json").write_text("{}", encoding="utf-8")
    settings = _FakeSettings(
        data_root=str(old),
        toolchain_paths={"jadx": str(tool)},
        mcp_servers=[{
            "id": "email-1",
            "name": "email",
            "command": str(old / "assistant_sessions" / "x.sandbox" / "bin.exe"),
            "args": ["stdio", str(old / "tools" / "cfg.json")],
            "cwd": str(old / "assistant_sessions"),
            "env": {},
            "enabled": True,
            "transport": "stdio",
        }],
        logging=LoggingSettings(log_dir=str(old / "logs")),
    )
    result = apply_data_root_change(settings, new, mode="copy")
    assert result["ok"] is True
    assert (new / "tasks" / "t1" / "task.json").is_file()
    assert (old / "tasks" / "t1" / "task.json").is_file()
    assert (new / "knowledge" / "note.md").is_file()
    updated = result["settings"]
    assert Path(updated.data_root) == new.resolve() or Path(updated.data_root) == new
    assert updated.toolchain_paths["jadx"].endswith(str(Path("tools") / "reverse" / "jadx.bat"))
    assert str(new) in updated.toolchain_paths["jadx"]
    assert str(new) in updated.mcp_servers[0]["command"]
    assert str(new) in updated.mcp_servers[0]["cwd"]
    assert any("jadx" in item["field"] for item in result["path_rewrites"])


def test_copy_reports_monotonic_byte_progress(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_tree(old)
    updates: list[dict] = []

    result = apply_data_root_change(
        _FakeSettings(data_root=str(old)),
        new,
        mode="copy",
        progress=lambda payload: updates.append(dict(payload)),
    )

    assert result["ok"] is True
    percents = [int(item.get("percent") or 0) for item in updates]
    assert percents == sorted(percents)
    assert any(item.get("phase") == "copying" for item in updates)
    assert any(item.get("phase") == "verifying" for item in updates)
    assert any(int(item.get("completed_bytes") or 0) > 0 for item in updates)


def test_move_removes_source_subdirs(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_tree(old)
    settings = _FakeSettings(data_root=str(old))
    result = apply_data_root_change(settings, new, mode="move")
    assert result["ok"] is True
    assert (new / "tasks" / "t1" / "task.json").is_file()
    # Core preparation cannot remove source data before the caller has saved
    # the updated settings pointer.
    assert (old / "tasks" / "t1" / "task.json").is_file()
    cleanup = finalize_data_root_move(result)
    assert cleanup["ok"] is True
    assert not (old / "tasks").exists()
    assert not (old / "knowledge").exists()


def test_switch_does_not_transfer_files(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_tree(old)
    new.mkdir()
    (new / "tools").mkdir()
    (new / "tools" / "jadx").write_text("x", encoding="utf-8")
    settings = _FakeSettings(
        data_root=str(old),
        toolchain_paths={"jadx": str(old / "tools" / "jadx")},
    )
    result = apply_data_root_change(settings, new, mode="switch")
    assert result["ok"] is True
    assert result["transferred"] == []
    assert not (new / "tasks").exists() or not any((new / "tasks").iterdir())
    assert (old / "tasks" / "t1" / "task.json").is_file()
    assert str(new) in result["settings"].toolchain_paths["jadx"]


def test_switch_rejects_missing_destination_without_creating_it(tmp_path: Path) -> None:
    old = tmp_path / "old"
    missing = tmp_path / "missing"
    _write_tree(old)
    result = apply_data_root_change(_FakeSettings(data_root=str(old)), missing, mode="switch")
    assert result["ok"] is False
    assert result["preview"]["blockers"][0]["code"] == "switch_destination_missing"
    assert not missing.exists()


def test_switch_rejects_rewrite_to_missing_destination(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_tree(old)
    new.mkdir()
    tool = old / "tools" / "jadx"
    tool.parent.mkdir(exist_ok=True)
    tool.write_text("x", encoding="utf-8")
    result = apply_data_root_change(
        _FakeSettings(data_root=str(old), toolchain_paths={"jadx": str(tool)}),
        new,
        mode="switch",
    )
    assert result["ok"] is False
    assert result["path_rewrite_skipped"][0]["field"] == "toolchain.paths.jadx"
    assert not (new / "tools").exists()


def test_failed_staged_copy_keeps_source_and_rolls_back_destination(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_tree(old)
    settings = _FakeSettings(data_root=str(old))
    real_copy = __import__(
        "argusai.core.workspace.data_root_migrate",
        fromlist=["_copy_tree"],
    )._copy_tree
    calls = 0

    def fail_second_copy(source: Path, destination: Path, **kwargs) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated transfer failure")
        real_copy(source, destination, **kwargs)

    with mock.patch(
        "argusai.core.workspace.data_root_migrate._copy_tree",
        side_effect=fail_second_copy,
    ):
        result = apply_data_root_change(settings, new, mode="move")

    assert result["ok"] is False
    assert (old / "tasks" / "t1" / "task.json").is_file()
    assert not new.exists()


def test_copy_skips_pycache_and_locked_runtime_files(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_tree(old)
    runtime = old / "tools" / "runtimes" / "python" / "Lib" / "encodings"
    pycache = runtime / "__pycache__"
    pycache.mkdir(parents=True)
    (runtime / "utf_8_sig.py").write_text("encoding", encoding="utf-8")
    locked_pyc = pycache / "utf_8_sig.cpython-312.pyc"
    locked_pyc.write_bytes(b"pyc")
    locked = old / "tools" / "runtimes" / "python" / "locked.bin"
    locked.write_bytes(b"busy")
    (old / "tools" / "runtimes" / "python" / "python.exe").write_bytes(b"exe")

    real_copy2 = __import__("shutil").copy2

    def copy2_locked(src, dst, *args, **kwargs):
        if Path(src).name == "locked.bin":
            raise PermissionError(13, "Permission denied", str(src))
        return real_copy2(src, dst, *args, **kwargs)

    preview = preview_data_root_change(old, new)
    assert any(w["code"] == "in_use_runtime_files" for w in preview["warnings"])

    with mock.patch("shutil.copy2", side_effect=copy2_locked):
        result = apply_data_root_change(_FakeSettings(data_root=str(old)), new, mode="copy")

    assert result["ok"] is True, result
    dest_python = new / "tools" / "runtimes" / "python"
    assert (dest_python / "python.exe").is_file()
    assert (dest_python / "Lib" / "encodings" / "utf_8_sig.py").is_file()
    assert not (dest_python / "Lib" / "encodings" / "__pycache__").exists()
    assert not (dest_python / "locked.bin").exists()
    skipped = next(row["skipped"] for row in result["transferred"] if row["name"] == "tools")
    assert any(item.endswith("locked.bin") for item in skipped)


def test_move_cleanup_ignores_leftover_pycache(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_tree(old)
    pycache = old / "tools" / "runtimes" / "python" / "__pycache__"
    pycache.mkdir(parents=True)
    (pycache / "utf_8_sig.cpython-312.pyc").write_bytes(b"pyc")
    (old / "tools" / "runtimes" / "python" / "python.exe").write_bytes(b"exe")

    result = apply_data_root_change(_FakeSettings(data_root=str(old)), new, mode="move")
    assert result["ok"] is True, result
    cleanup = finalize_data_root_move(result)
    assert cleanup["ok"] is True, cleanup
    assert (new / "tools" / "runtimes" / "python" / "python.exe").is_file()
    assert not (new / "tools" / "runtimes" / "python" / "__pycache__").exists()


def test_copy_skips_node_module_tests_and_retries_invalid_metadata(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_tree(old)
    runtime = old / "tools" / "runtimes" / "node" / "tools" / "node_modules" / "restringer"
    (runtime / "src").mkdir(parents=True)
    (runtime / "src" / "restringer.js").write_text("module.exports = {}", encoding="utf-8")
    (runtime / "src" / "weird.js").write_text("ok", encoding="utf-8")
    tests_dir = runtime / "tests" / "resources"
    tests_dir.mkdir(parents=True)
    (tests_dir / "ant.js-deob.js").write_text("fixture", encoding="utf-8")

    real_copy2 = __import__("shutil").copy2

    def copy2_einval(src, dst, *args, **kwargs):
        name = Path(str(src).replace("\\\\?\\", "")).name
        if name == "weird.js":
            raise OSError(22, "Invalid argument", str(src))
        return real_copy2(src, dst, *args, **kwargs)

    with mock.patch("shutil.copy2", side_effect=copy2_einval):
        result = apply_data_root_change(_FakeSettings(data_root=str(old)), new, mode="copy")

    assert result["ok"] is True, result
    dest = new / "tools" / "runtimes" / "node" / "tools" / "node_modules" / "restringer"
    assert (dest / "src" / "restringer.js").is_file()
    assert (dest / "src" / "weird.js").is_file()
    assert not (dest / "tests").exists()


def test_copy_blocked_when_destination_has_conflict(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_tree(old)
    (new / "tasks" / "other").mkdir(parents=True)
    (new / "tasks" / "other" / "x.txt").write_text("x", encoding="utf-8")
    settings = _FakeSettings(data_root=str(old))
    result = apply_data_root_change(settings, new, mode="copy")
    assert result["ok"] is False
    assert "tasks" in (result.get("error") or "")


def test_save_settings_ignores_data_root_pointer_flip(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from argusai.gui.settings import SettingsMixin

    class _Api(SettingsMixin):
        pass

    old = tmp_path / "old"
    old.mkdir()
    current = MagicMock()
    current.to_dict.return_value = {
        "ai": {"enabled": True, "endpoints": [], "auxiliary": {}},
        "proxy": {},
        "security": {},
        "browser": {},
        "toolchain": {"paths": {}},
        "runtime": {"mirrors": {}},
        "oob": {},
        "assistant": {},
        "logging": {},
        "mcp": {"servers": []},
        "user_habits": {},
        "data_root": str(old),
        "kb_trash_retention_days": 30,
    }
    # Provide attribute access used while building AppSettings from current dict path —
    # save_settings uses current.to_dict() only for merging sections.
    saved = {}

    mgr = SimpleNamespace(
        get=lambda: SimpleNamespace(
            to_dict=current.to_dict,
            data_root=str(old),
        ),
        save=lambda s: saved.setdefault("settings", s),
    )
    # Real save_settings calls settings_mgr.get().to_dict() — wire that.
    real_settings = None

    def _get():
        nonlocal real_settings
        if real_settings is None:
            from argusai.core.settings import AppSettings

            real_settings = AppSettings(data_root=str(old))
        return real_settings

    def _save(s):
        nonlocal real_settings
        real_settings = s
        saved["settings"] = s

    api = _Api()
    api.settings_mgr = SimpleNamespace(get=_get, save=_save)
    out = api.save_settings({"data_root": str(tmp_path / "elsewhere"), "ai": {"enabled": True, "endpoints": []}})
    assert out["ok"] is True
    assert Path(saved["settings"].data_root) == old.resolve() or saved["settings"].data_root == str(old)


def test_gui_migration_refuses_active_assistant_stream(tmp_path: Path) -> None:
    from argusai.gui.settings import SettingsMixin

    class _Api(SettingsMixin):
        pass

    api = _Api()
    api._assistant_streams = {"run-1": {"done": False}}
    api._assistant_running_sessions = set()
    out = api.apply_data_root_change(str(tmp_path / "new"), mode="copy")
    assert out["ok"] is False
    assert str(out["error"]).startswith("data_root_busy:")


def test_gui_migration_copy_persists_new_data_root(tmp_path: Path) -> None:
    from argusai.core.settings import AppSettings, SettingsManager
    from argusai.gui.settings import SettingsMixin

    old = tmp_path / "old"
    new = tmp_path / "new"
    _write_tree(old)
    mgr = SettingsManager(path=tmp_path / "settings.json")
    mgr.save(AppSettings(data_root=str(old)))

    class _Api(SettingsMixin):
        pass

    api = _Api()
    api.settings_mgr = mgr
    api._assistant_streams = {}
    api._assistant_running_sessions = set()
    out = api.apply_data_root_change(str(new), mode="copy")
    assert out["ok"] is True, out
    assert Path(mgr.get().data_root) == new.resolve()
    assert (new / "tasks" / "t1" / "task.json").is_file()
    assert (old / "tasks" / "t1" / "task.json").is_file()
