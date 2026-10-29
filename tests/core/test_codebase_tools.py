import asyncio
from pathlib import Path
import sys

from argusai.core import _tools


class DummyApi:
    pass


def test_codebase_scope_tree_search_and_read(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    src = root / "src"
    src.mkdir(parents=True)
    (src / "app.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/orders/{order_id}')\n"
        "def read_order(order_id: str):\n"
        "    return {'order_id': order_id}\n",
        encoding="utf-8",
    )
    (root / "node_modules").mkdir()
    (root / "node_modules" / "ignored.js").write_text("secret = true\n", encoding="utf-8")
    api = DummyApi()

    scope = _tools.codebase_scope_set(api, root=str(root), audit_goal="authz review", _session_id="s1")
    assert scope["ok"] is True

    tree = _tools.codebase_tree(api, max_depth=2, _session_id="s1")
    paths = {entry["path"] for entry in tree["entries"]}
    assert "src/app.py" in paths
    assert not any(path.startswith("node_modules") for path in paths)

    hits = _tools.codebase_search(api, pattern="read_order", path=".", _session_id="s1")
    assert hits["ok"] is True
    assert hits["matches"][0]["relative_path"] == "src/app.py"

    content = _tools.codebase_read(api, path="src/app.py", offset=3, limit=2, _session_id="s1")
    assert content["ok"] is True
    assert "orders" in content["content"]
    assert content["relative_path"] == "src/app.py"


def test_codebase_rejects_scope_escape(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("nope", encoding="utf-8")
    api = DummyApi()
    _tools.codebase_scope_set(api, root=str(root), _session_id="s1")

    result = _tools.codebase_read(api, path=str(outside), _session_id="s1")

    assert result["ok"] is False
    assert "escapes codebase scope" in result["error"]


def test_codebase_write_patch_delete_polyglot(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    api = DummyApi()
    assert _tools.codebase_write_file(api, path="src/a.ts", content="export const x = 1\n", _session_id="s1")["ok"] is False

    _tools.codebase_scope_set(api, root=str(root), _session_id="s1")
    written = _tools.codebase_write_file(
        api,
        path="src/util.ts",
        content="export const hello = 'world'\n",
        _session_id="s1",
    )
    assert written["ok"] is True
    assert (root / "src" / "util.ts").read_text(encoding="utf-8") == "export const hello = 'world'\n"

    patched = _tools.codebase_patch_file(
        api,
        path="src/util.ts",
        old_string="world",
        new_string="argus",
        _session_id="s1",
    )
    assert patched["ok"] is True
    assert "argus" in (root / "src" / "util.ts").read_text(encoding="utf-8")

    go = _tools.codebase_write_file(
        api,
        path="pkg/main.go",
        content="package main\n\nfunc main() {}\n",
        _session_id="s1",
    )
    assert go["ok"] is True

    deleted = _tools.codebase_delete_file(api, path="pkg/main.go", _session_id="s1")
    assert deleted["ok"] is True
    assert not (root / "pkg" / "main.go").exists()


def test_codebase_write_guards(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "node_modules").mkdir()
    api = DummyApi()
    _tools.codebase_scope_set(api, root=str(root), _session_id="s1")

    excluded = _tools.codebase_write_file(
        api,
        path="node_modules/x.js",
        content=" modular.exports = 1\n",
        _session_id="s1",
    )
    assert excluded["ok"] is False
    assert excluded.get("error_code") == "codebase_path_excluded"

    secret = _tools.codebase_write_file(
        api,
        path=".env",
        content="SECRET=1\n",
        _session_id="s1",
    )
    assert secret["ok"] is False
    assert secret.get("error_code") == "codebase_secret_blocked"

    binary = _tools.codebase_write_file(
        api,
        path="logo.png",
        content="not-really-png",
        _session_id="s1",
    )
    assert binary["ok"] is False
    assert binary.get("error_code") == "codebase_binary_blocked"

    outside = tmp_path / "out.ts"
    escape = _tools.codebase_write_file(
        api,
        path=str(outside),
        content="export {}\n",
        _session_id="s1",
    )
    assert escape["ok"] is False
    assert "escapes" in escape["error"]

    # Governance paths must stay writable (not excluded / allowlisted).
    cursor_rule = _tools.codebase_write_file(
        api,
        path=".cursor/rules/agents.md",
        content="# Agent rules\n",
        _session_id="s1",
    )
    assert cursor_rule["ok"] is True
    cursorrules = _tools.codebase_write_file(
        api,
        path=".cursorrules",
        content="# cursor rules\n",
        _session_id="s1",
    )
    assert cursorrules["ok"] is True


def test_codebase_run_python_and_shell_cwd(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    api = DummyApi()
    _tools.codebase_scope_set(api, root=str(root), _session_id="s1")
    _tools.codebase_write_file(api, path="marker.txt", content="ok-marker\n", _session_id="s1")

    py = asyncio.run(
        _tools.codebase_run_python_snippet(
            api,
            code="from pathlib import Path\nprint(Path('marker.txt').read_text(encoding='utf-8').strip())\n",
            _session_id="s1",
        )
    )
    assert py["ok"] is True
    assert py["cwd"] == str(root.resolve())
    assert "ok-marker" in (py.get("stdout_excerpt") or "")

    if sys.platform.startswith("win"):
        script = (
            f'& "{sys.executable}" -c '
            "\"from pathlib import Path; print(Path('marker.txt').read_text(encoding='utf-8').strip())\""
        )
        shell_name = "powershell"
    else:
        script = (
            f"'{sys.executable}' -c "
            "\"from pathlib import Path; print(Path('marker.txt').read_text(encoding='utf-8').strip())\""
        )
        shell_name = "bash"
    shell = asyncio.run(
        _tools.codebase_run_shell_snippet(
            api,
            script=script,
            shell=shell_name,
            _session_id="s1",
        )
    )
    assert shell.get("cwd") == str(root.resolve())
    if shell.get("ok"):
        assert "ok-marker" in (shell.get("stdout_excerpt") or "")


def test_codebase_run_rejects_without_scope() -> None:
    api = DummyApi()
    r = asyncio.run(_tools.codebase_run_python_snippet(api, code="print(1)\n", _session_id="s1"))
    assert r["ok"] is False
    assert "scope" in r["error"]


def test_session_project_dir_binds_codebase_scope(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from argusai.core.agent_runtimes import normalize_session_runtime_binding

    root = tmp_path / "repo"
    src = root / "src"
    src.mkdir(parents=True)
    (src / "app.py").write_text("x = 1\n", encoding="utf-8")
    api = DummyApi()
    api._assistant_sessions = {
        "s1": SimpleNamespace(
            runtime_binding=normalize_session_runtime_binding({
                "workspace_mode": "project",
                "project_dir": str(root),
            })
        )
    }
    got = _tools.codebase_scope_get(api, _session_id="s1")
    assert got["ok"] is True
    assert Path(got["root"]) == root.resolve()
    tree = _tools.codebase_tree(api, max_depth=2, _session_id="s1")
    paths = {entry["path"] for entry in tree["entries"]}
    assert "src/app.py" in paths


def test_explicit_codebase_scope_overrides_session_project(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from argusai.core.agent_runtimes import normalize_session_runtime_binding

    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    (other / "only.txt").write_text("hi\n", encoding="utf-8")
    api = DummyApi()
    api._assistant_sessions = {
        "s1": SimpleNamespace(
            runtime_binding=normalize_session_runtime_binding({
                "workspace_mode": "project",
                "project_dir": str(project),
            })
        )
    }
    _tools.codebase_scope_set(api, root=str(other), _session_id="s1")
    got = _tools.codebase_scope_get(api, _session_id="s1")
    assert Path(got["root"]) == other.resolve()
