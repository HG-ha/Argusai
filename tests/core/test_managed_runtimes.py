from __future__ import annotations

import hashlib
import asyncio
import json
import threading
import time
import zipfile
from pathlib import Path
from urllib.error import URLError

import pytest

from argusai.core import managed_runtimes as mr


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Windows", "AMD64", "win-x64"),
        ("Windows", "ARM64", "win-arm64"),
        ("Linux", "x86_64", "linux-x64"),
        ("Linux", "aarch64", "linux-arm64"),
        ("Darwin", "x86_64", "macos-x64"),
        ("Darwin", "arm64", "macos-arm64"),
    ],
)
def test_platform_subdir(system: str, machine: str, expected: str) -> None:
    assert mr.platform_subdir(system, machine) == expected


def test_windows_python_archive_keeps_verified_stripped_distribution() -> None:
    spec = mr.archive_spec("python", platform_key="win-x64")

    assert spec.filename.endswith("-install_only_stripped.tar.gz")
    assert spec.sha256 == "fa8ac308a7cd1774d599ad9a29f1e374fbdc11453b12a8c50cc4afdb5c4bfd1a"


def test_windows_python_venv_health_requires_launchers(tmp_path: Path) -> None:
    runtime = tmp_path / "python"
    (runtime / "Lib" / "venv" / "scripts" / "nt").mkdir(parents=True)
    (runtime / "Lib" / "struct.py").write_text("", encoding="utf-8")
    (runtime / "python.exe").write_bytes(b"python")

    damaged = mr.python_venv_health(runtime, platform_key="win-x64")
    assert damaged["ok"] is False
    assert any(
        path.endswith(("Lib\\venv\\scripts\\nt\\python.exe", "Lib/venv/scripts/nt/python.exe"))
        for path in damaged["missing"]
    )

    for relative in (
        "Lib/venv/scripts/nt/python.exe",
        "Lib/venv/scripts/nt/pythonw.exe",
    ):
        path = runtime / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"launcher")

    assert mr.python_venv_health(runtime, platform_key="win-x64")["ok"] is True


def test_runtime_mirror_settings_round_trip() -> None:
    from argusai.core.settings import AppSettings, _parse

    original = AppSettings(runtime_mirrors={
        "python": "https://mirror.example/python",
        "npm": "https://registry.example",
    })
    restored = _parse(original.to_dict())

    assert restored.runtime_mirrors == original.runtime_mirrors


def test_runtime_download_trusts_the_certifi_bundle() -> None:
    context = mr._tls_context()
    stats = context.cert_store_stats()
    assert stats["x509_ca"] > 10


def test_download_verified_falls_back_only_after_network_error(tmp_path: Path) -> None:
    payload = b"verified-runtime"
    expected = hashlib.sha256(payload).hexdigest()
    calls: list[str] = []

    def downloader(url: str, path: Path) -> None:
        calls.append(url)
        if url.endswith("first"):
            raise URLError("offline")
        path.write_bytes(payload)

    result = mr.download_verified(
        ["https://mirror.invalid/first", "https://mirror.invalid/second"],
        tmp_path / "runtime.zip",
        expected,
        downloader=downloader,
    )

    assert result["source"].endswith("second")
    assert calls == [
        "https://mirror.invalid/first",
        "https://mirror.invalid/first",
        "https://mirror.invalid/second",
    ]


def test_download_verified_stops_on_checksum_mismatch(tmp_path: Path) -> None:
    calls: list[str] = []
    # Shaped like a real gzip archive, so the only thing wrong is the content.
    tampered = b"\x1f\x8b" + b"tampered"

    def downloader(url: str, path: Path) -> None:
        calls.append(url)
        path.write_bytes(tampered)

    with pytest.raises(mr.ChecksumMismatch):
        mr.download_verified(
            ["https://mirror.invalid/first", "https://mirror.invalid/second"],
            tmp_path / "runtime.zip",
            hashlib.sha256(b"expected").hexdigest(),
            downloader=downloader,
        )

    assert calls == ["https://mirror.invalid/first"]


def test_download_verified_skips_a_proxy_that_serves_an_error_page(tmp_path: Path) -> None:
    """A dead GitHub proxy answers 200 with HTML; that is not tampering."""
    payload = b"\x1f\x8bverified-runtime"
    calls: list[str] = []

    def downloader(url: str, path: Path) -> None:
        calls.append(url)
        if url.endswith("proxy"):
            path.write_bytes(b"<!DOCTYPE html><title>404 Not Found</title>")
            return
        path.write_bytes(payload)

    result = mr.download_verified(
        ["https://mirror.invalid/proxy", "https://mirror.invalid/upstream"],
        tmp_path / "runtime.tar.gz",
        hashlib.sha256(payload).hexdigest(),
        downloader=downloader,
    )

    assert result["source"].endswith("upstream")
    assert calls[0].endswith("proxy")
    failed = [item for item in result["attempts"] if not item["ok"]]
    assert failed and all("not an archive" in item["error"] for item in failed)


def test_download_verified_accepts_an_uncompressed_tar_as_an_archive(tmp_path: Path) -> None:
    """The tar magic lives at offset 257, not at the start of the file."""
    body = bytearray(b"\0" * 512)
    body[257:262] = b"ustar"
    calls: list[str] = []

    def downloader(url: str, path: Path) -> None:
        calls.append(url)
        path.write_bytes(bytes(body))

    with pytest.raises(mr.ChecksumMismatch):
        mr.download_verified(
            ["https://mirror.invalid/first", "https://mirror.invalid/second"],
            tmp_path / "runtime.tar",
            hashlib.sha256(b"expected").hexdigest(),
            downloader=downloader,
        )

    assert calls == ["https://mirror.invalid/first"]


def test_safe_extract_blocks_zip_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", "no")

    with pytest.raises(mr.RuntimeInstallError, match="path traversal"):
        mr._safe_extract(archive, tmp_path / "extract")

    assert not (tmp_path / "outside.txt").exists()


def test_atomic_replace_rolls_back_on_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "python"
    destination.mkdir()
    (destination / "state.txt").write_text("old", encoding="utf-8")
    payload = tmp_path / "new-python"
    payload.mkdir()
    (payload / "state.txt").write_text("new", encoding="utf-8")

    real_replace = mr.os.replace
    call_count = 0

    def flaky_replace(source: str | Path, target: str | Path) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise OSError("simulated commit failure")
        real_replace(source, target)

    monkeypatch.setattr(mr.os, "replace", flaky_replace)
    with pytest.raises(OSError, match="simulated"):
        mr._atomic_replace(payload, destination)

    assert (destination / "state.txt").read_text(encoding="utf-8") == "old"


def test_node_package_retry_uses_fresh_directory_and_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_root = tmp_path / "node"
    node_root.mkdir()
    (node_root / "node.exe").write_bytes(b"node")
    npm_cli = node_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    npm_cli.parent.mkdir(parents=True)
    npm_cli.write_text("", encoding="utf-8")

    assets = tmp_path / "assets"
    assets.mkdir()
    package_file = assets / "package.json"
    lock_file = assets / "package-lock.json"
    package_file.write_text('{"private":true}', encoding="utf-8")
    lock_file.write_text('{"lockfileVersion":3}', encoding="utf-8")
    monkeypatch.setattr(mr, "node_tools_package_file", lambda: package_file)
    monkeypatch.setattr(mr, "node_tools_lockfile", lambda: lock_file)
    monkeypatch.setattr(
        mr,
        "npm_registries",
        lambda _mirrors=None: ["https://registry.npmjs.org"],
    )

    working_dirs: list[Path] = []
    caches: list[str] = []

    def fake_run(args, *, cwd, env, **_kwargs):  # noqa: ANN001
        working_dir = Path(cwd)
        working_dirs.append(working_dir)
        caches.append(env["npm_config_cache"])
        modules = working_dir / "node_modules"
        modules.mkdir()
        if len(working_dirs) == 1:
            (modules / "failed.txt").write_text("partial", encoding="utf-8")
            return mr.subprocess.CompletedProcess(
                args,
                1,
                stdout=b"",
                stderr=b"npm error code EPERM",
            )
        (modules / "ready.txt").write_text("ok", encoding="utf-8")
        return mr.subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(mr.subprocess, "run", fake_run)

    result = mr._install_node_packages(node_root, tmp_path / "staging", {})

    assert result["source"] == "https://registry.npmjs.org"
    assert len(result["attempts"]) == 2
    assert working_dirs[0] != working_dirs[1]
    assert caches[0] != caches[1]
    assert (node_root / "tools" / "node_modules" / "ready.txt").is_file()
    assert not (node_root / "tools" / "node_modules" / "failed.txt").exists()


def test_install_reuses_then_repairs_managed_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "fixture-python.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for relative in (
            "python.exe",
            "venvlauncher.exe",
            "venvwlauncher.exe",
            "Lib/venv/scripts/nt/python.exe",
            "Lib/venv/scripts/nt/pythonw.exe",
        ):
            bundle.writestr(f"python/{relative}", b"fixture-runtime")
        bundle.writestr("python/Lib/struct.py", "")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    spec = mr.ArchiveSpec(
        runtime="python",
        version=mr.PYTHON_VERSION,
        platform="win-x64",
        filename=archive.name,
        sha256=digest,
        approx_download_bytes=archive.stat().st_size,
    )
    monkeypatch.setattr(mr, "archive_spec", lambda *_args, **_kwargs: spec)
    monkeypatch.setattr(
        mr,
        "_probe_version",
        lambda *_args, **_kwargs: (True, f"Python {mr.PYTHON_VERSION}", ""),
    )

    def downloader(_url: str, destination: Path) -> None:
        destination.write_bytes(archive.read_bytes())

    tools_dir = tmp_path / "tools"
    first = mr.install_runtime("python", tools_dir=tools_dir, downloader=downloader)
    reused = mr.install_runtime("python", tools_dir=tools_dir, downloader=downloader)
    mr.python_executable(tools_dir).unlink()
    repaired = mr.install_runtime("python", tools_dir=tools_dir, downloader=downloader)

    assert first["ok"] is True and first["installed_now"] is True
    assert reused["ok"] is True and reused["already_installed"] is True
    assert repaired["ok"] is True and repaired["installed_now"] is True
    assert mr.python_executable(tools_dir).read_bytes() == b"fixture-runtime"


def test_runtime_status_rejects_an_outdated_archive_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = mr.ArchiveSpec(
        runtime="python",
        version=mr.PYTHON_VERSION,
        platform="win-x64",
        filename="python.tar.gz",
        sha256="b" * 64,
        approx_download_bytes=1,
    )
    tools_dir = tmp_path / "tools"
    root = mr.runtime_root("python", tools_dir)
    root.mkdir(parents=True)
    (root / "python.exe").write_bytes(b"python")
    (root / mr._METADATA_FILE).write_text(
        json.dumps({"archive_sha256": "a" * 64}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mr, "archive_spec", lambda *_args, **_kwargs: spec)
    monkeypatch.setattr(
        mr,
        "_probe_version",
        lambda *_args, **_kwargs: (True, f"Python {mr.PYTHON_VERSION}", ""),
    )
    monkeypatch.setattr(
        mr,
        "python_venv_health",
        lambda *_args, **_kwargs: {"ok": True, "missing": []},
    )

    status = mr.runtime_status("python", tools_dir)

    assert status["healthy"] is False
    assert status["status"] == "damaged"
    assert "archive pin changed" in status["error"]


def test_install_lock_serializes_threads(tmp_path: Path) -> None:
    lock = tmp_path / ".python.install.lock"
    entered: list[str] = []
    first_ready = threading.Event()

    def first() -> None:
        with mr._InstallLock(lock, timeout=2):
            entered.append("first")
            first_ready.set()
            time.sleep(0.15)

    def second() -> None:
        first_ready.wait(1)
        with mr._InstallLock(lock, timeout=2):
            entered.append("second")

    one = threading.Thread(target=first)
    two = threading.Thread(target=second)
    one.start()
    two.start()
    one.join()
    two.join()

    assert entered == ["first", "second"]
    assert not lock.exists()


def test_python_sandbox_never_falls_back_to_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argusai.core.workspace import session_sandbox

    root = tmp_path / "data" / "assistant_sessions" / "session.sandbox"
    root.mkdir(parents=True)
    (root / "probe.py").write_text("print('must not run')", encoding="utf-8")
    monkeypatch.setattr(
        session_sandbox,
        "managed_runtime_status",
        lambda *_args, **_kwargs: {
            "ok": True,
            "healthy": False,
            "status": "missing",
            "runtime": "python",
            "error": "not installed",
        },
    )
    monkeypatch.setattr(
        session_sandbox,
        "_direct_python_command",
        lambda *_args, **_kwargs: pytest.fail("host fallback was consulted"),
    )

    result = asyncio.run(session_sandbox.run_python_script(root, "probe.py"))

    assert result["ok"] is False
    assert result["error_code"] == "managed_runtime_required"
    assert result["runtime"] == "python"


def test_mcp_bare_node_uses_managed_runtime_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argusai.core.infra import mcp_client

    root = tmp_path / "node"
    binary = root / ("node.exe" if mr.os.name == "nt" else "bin/node")
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"node")
    monkeypatch.setattr(mcp_client, "_managed_node_dir", lambda _tools="": str(root))

    command, source, runtime_dir = mcp_client._resolve_node_launch_command(
        "node",
        {},
        str(tmp_path / "tools"),
    )

    assert command == str(binary)
    assert source == "managed"
    assert runtime_dir == str(binary.parent)


def test_missing_runtime_uses_one_confirmation_then_continues_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import ToolRegistry

    state = {"ready": False, "installs": 0, "runs": 0}

    class SettingsManager:
        def get(self) -> SimpleNamespace:
            return SimpleNamespace(
                data_root=str(tmp_path),
                runtime_mirrors={},
                assistant_enable_web_search=False,
            )

    class Api:
        settings_mgr = SettingsManager()

    class Ai:
        def __init__(self) -> None:
            self.calls = 0

        async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                return {
                    "choices": [{
                        "message": {
                            "content": "",
                            "tool_calls": [{
                                "id": "python-call",
                                "type": "function",
                                "function": {
                                    "name": "run_python_snippet",
                                    "arguments": json.dumps({"code": "print('ok')"}),
                                },
                            }],
                        },
                    }],
                }
            return {"choices": [{"message": {"content": "finished", "tool_calls": []}}]}

    async def run_python_snippet(code: str = "") -> dict[str, object]:
        state["runs"] += 1
        return {"ok": True, "stdout": "ok\n", "code": code}

    def fake_install(runtime: str, **kwargs: object) -> dict[str, object]:
        state["installs"] += 1
        state["ready"] = True
        return {"ok": True, "runtime": runtime, "source": "fixture://python"}

    monkeypatch.setattr(mr, "install_runtime", fake_install)
    registry = ToolRegistry(api=Api(), session_id="session-1")
    registry.register(
        "run_python_snippet",
        "run Python",
        {"code": {"type": "string"}},
        run_python_snippet,
        required=["code"],
    )
    session = AssistantSession(registry=registry, ai_provider=Ai())
    monkeypatch.setattr(session, "_runtime_is_ready", lambda _runtime: state["ready"])

    first = asyncio.run(session.chat("run it", thinking_enabled=False))

    assert first.status == "awaiting_confirmation"
    assert len(first.blocks) == 1
    assert first.blocks[0]["title"] == "安装 Python 沙盒环境？"
    assert "run_python_snippet" in first.blocks[0]["body"]
    pending_id, version = session._ensure_pending_identity()

    second = asyncio.run(session.chat(
        "",
        thinking_enabled=False,
        confirmation_action=True,
        confirmation={
            "type": "batch_confirmation_decision",
            "pending_id": pending_id,
            "version": version,
            "decisions": {"python-call": "allow"},
        },
    ))

    assert second.status == "done"
    assert state == {"ready": True, "installs": 1, "runs": 1}


def test_runtime_confirmation_denial_never_dispatches_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import ToolRegistry

    class SettingsManager:
        def get(self) -> SimpleNamespace:
            return SimpleNamespace(
                data_root=str(tmp_path),
                runtime_mirrors={},
                assistant_enable_web_search=False,
            )

    class Api:
        settings_mgr = SettingsManager()

    class Ai:
        async def chat(self, messages=None, tools=None, thinking_enabled=None):  # noqa: ANN001
            return {
                "choices": [{
                    "message": {
                        "content": "",
                        "tool_calls": [{
                            "id": "node-call",
                            "type": "function",
                            "function": {
                                "name": "run_node_script",
                                "arguments": json.dumps({"path": "probe.js"}),
                            },
                        }],
                    },
                }],
            }

    async def should_not_run(**kwargs: object) -> dict[str, object]:
        pytest.fail("original tool ran after runtime install was denied")

    registry = ToolRegistry(api=Api(), session_id="session-2")
    registry.register("run_node_script", "run Node", {}, should_not_run)
    session = AssistantSession(registry=registry, ai_provider=Ai())
    monkeypatch.setattr(session, "_runtime_is_ready", lambda _runtime: False)

    first = asyncio.run(session.chat("run it", thinking_enabled=False))
    pending_id, version = session._ensure_pending_identity()
    second = asyncio.run(session.chat(
        "",
        thinking_enabled=False,
        confirmation_action=True,
        confirmation={
            "type": "batch_confirmation_decision",
            "pending_id": pending_id,
            "version": version,
            "decisions": {"node-call": "deny"},
        },
    ))

    assert first.status == "awaiting_confirmation"
    assert second.status == "cancelled"
    assert "原工具未执行" in second.text


def test_runtime_install_failure_blocks_original_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import ToolRegistry

    class SettingsManager:
        def get(self) -> SimpleNamespace:
            return SimpleNamespace(data_root=str(tmp_path), runtime_mirrors={})

    async def should_not_run(**kwargs: object) -> dict[str, object]:
        pytest.fail("original tool ran after runtime installation failed")

    registry = ToolRegistry(
        api=SimpleNamespace(settings_mgr=SettingsManager()),
        session_id="failure-session",
    )
    registry.register("run_python_snippet", "run Python", {}, should_not_run)
    session = AssistantSession(registry=registry, ai_provider=None)
    monkeypatch.setattr(session, "_runtime_is_ready", lambda _runtime: False)
    monkeypatch.setattr(
        mr,
        "install_runtime",
        lambda *_args, **_kwargs: {
            "ok": False,
            "runtime": "python",
            "error_code": "runtime_checksum_mismatch",
            "error": "fixture mismatch",
        },
    )

    result = asyncio.run(session._dispatch_tool(
        "run_python_snippet",
        {"code": "print('must not run')"},
        user_approved=True,
        approved_runtime="python",
    ))

    assert result["ok"] is False
    assert result["blocked"] is True
    assert result["error_code"] == "runtime_checksum_mismatch"


@pytest.mark.parametrize(
    ("platform_key", "filename"),
    [
        ("win-x64", "wabt-1.0.36-windows.tar.gz"),
        ("linux-x64", "wabt-1.0.36-ubuntu-20.04.tar.gz"),
        ("macos-x64", "wabt-1.0.36-macos-12.tar.gz"),
        ("macos-arm64", "wabt-1.0.36-macos-14.tar.gz"),
    ],
)
def test_wabt_archive_is_pinned_per_platform(platform_key: str, filename: str) -> None:
    spec = mr.archive_spec("wabt", platform_key=platform_key)

    assert spec.filename == filename
    assert len(spec.sha256) == 64


def test_wabt_has_no_linux_arm64_build() -> None:
    """WABT publishes no arm64 Linux binary; installing the x64 one would lie."""
    with pytest.raises(mr.RuntimeInstallError, match="linux-arm64"):
        mr.archive_spec("wabt", platform_key="linux-arm64")


def test_wabt_sources_prefer_the_upstream_release() -> None:
    spec = mr.archive_spec("wabt", platform_key="win-x64")

    sources = mr.archive_sources(spec)

    assert sources[0].startswith("https://github.com/WebAssembly/wabt/releases/download/1.0.36/")
    assert len(sources) > 1


@pytest.mark.parametrize("runtime", ["python", "node", "wabt"])
def test_upstream_is_always_the_first_source(runtime: str) -> None:
    """Mirrors are fallbacks; the vendor's own host stays the trust anchor."""
    spec = mr.archive_spec(runtime, platform_key="win-x64")
    upstream = {
        "python": "https://github.com/astral-sh/",
        "node": "https://nodejs.org/dist/",
        "wabt": "https://github.com/WebAssembly/",
    }[runtime]

    sources = mr.archive_sources(spec)

    assert sources[0].startswith(upstream)
    assert len(sources) == len(set(sources))


@pytest.mark.parametrize("runtime", ["python", "wabt"])
def test_github_hosted_runtimes_get_the_china_accelerators(runtime: str) -> None:
    spec = mr.archive_spec(runtime, platform_key="win-x64")

    sources = mr.archive_sources(spec)

    for prefix in mr._GITHUB_ACCELERATORS:
        assert any(url.startswith(prefix) for url in sources), prefix
    # Every accelerator re-serves a full github.com URL, so the upstream path
    # stays embedded and a dropped mirror never silently rewrites the asset.
    for url in sources[1:]:
        assert url.count("https://github.com/") == 1
        assert url.endswith(sources[0].split("/")[-1])


def test_node_does_not_use_github_accelerators() -> None:
    """Node ships from nodejs.org, so it uses distro mirrors, not gh proxies."""
    spec = mr.archive_spec("node", platform_key="win-x64")

    sources = mr.archive_sources(spec)

    assert not any("github.com" in url for url in sources)
    assert all(url.endswith(spec.filename) for url in sources)


def test_custom_mirrors_outrank_the_builtin_accelerators() -> None:
    spec = mr.archive_spec("wabt", platform_key="win-x64")

    sources = mr.archive_sources(spec, {"wabt": "https://mirror.example/wabt"})

    assert sources[0].startswith("https://mirror.example/")
    assert sources[1].startswith("https://github.com/")


def test_wabt_mirror_setting_is_honoured() -> None:
    spec = mr.archive_spec("wabt", platform_key="win-x64")

    sources = mr.archive_sources(spec, {"wabt": "https://mirror.example/wabt"})

    assert sources[0] == f"https://mirror.example/wabt/1.0.36/{spec.filename}"


def test_wasm_disassemble_declares_a_wabt_dependency(tmp_path: Path) -> None:
    """wasm_disassemble shells out to WABT, so it must gate on the install."""
    from types import SimpleNamespace

    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import ToolRegistry

    class SettingsManager:
        def get(self) -> SimpleNamespace:
            return SimpleNamespace(data_root=str(tmp_path), runtime_mirrors={})

    session = AssistantSession(
        registry=ToolRegistry(
            api=SimpleNamespace(settings_mgr=SettingsManager()),
            session_id="wabt-session",
        ),
        ai_provider=None,
    )

    assert session._runtime_dependency("wasm_disassemble", {}) == "wabt"
    assert session._runtime_dependency("wasm_decompile", {}) == "wabt"
    # Pure-Python wasm tools must keep working with no WABT installed.
    assert session._runtime_dependency("wasm_inspect", {}) == ""
    assert session._runtime_dependency("wasm_strings", {}) == ""


def test_wabt_confirmation_card_does_not_promise_a_sandbox(tmp_path: Path) -> None:
    """WABT is a CLI toolchain; the card must not claim per-session isolation."""
    from types import SimpleNamespace

    from argusai.core.assistant.session import AssistantSession
    from argusai.core.assistant.tools import ToolRegistry

    class SettingsManager:
        def get(self) -> SimpleNamespace:
            return SimpleNamespace(data_root=str(tmp_path), runtime_mirrors={})

    session = AssistantSession(
        registry=ToolRegistry(
            api=SimpleNamespace(settings_mgr=SettingsManager()),
            session_id="wabt-session",
        ),
        ai_provider=None,
    )

    block = session._confirm_block(
        "wasm_decompile",
        {"path": "a.wasm"},
        confirm_id="wabt-call",
        runtime="wabt",
        covered_tool_names=["wasm_decompile"],
    )

    assert block["title"] == "安装 WABT WebAssembly 工具链？"
    assert "沙盒环境" not in block["title"]
    assert "当前会话隔离目录" not in block["body"]
    assert mr.WABT_VERSION in block["body"]
    assert "wasm_decompile" in block["body"]

    python_block = session._confirm_block(
        "run_python_snippet",
        {"code": "print(1)"},
        confirm_id="python-call",
        runtime="python",
        covered_tool_names=["run_python_snippet"],
    )

    assert python_block["title"] == "安装 Python 沙盒环境？"
    assert "当前会话隔离目录" in python_block["body"]


def test_vendor_assets_dir_prefers_macos_app_resources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """build.py copies runners into Contents/Resources; install must find them."""
    contents = tmp_path / "ArgusAI.app" / "Contents"
    core_dir = contents / "MacOS" / "argusai" / "core"
    core_dir.mkdir(parents=True)
    runners = contents / "Resources" / "argusai" / "vendor" / "runners"
    runners.mkdir(parents=True)
    (runners / "package.json").write_text("{}", encoding="utf-8")
    (runners / "package-lock.json").write_text("{}", encoding="utf-8")

    fake_module = core_dir / "managed_runtimes.py"
    fake_module.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(mr, "__file__", str(fake_module))
    monkeypatch.setattr(mr.sys, "platform", "darwin")

    assert mr.vendor_assets_dir() == contents / "Resources" / "argusai" / "vendor"
    assert mr.node_tools_package_file().is_file()
    assert mr.node_tools_lockfile().is_file()


def test_vendor_assets_dir_falls_back_beside_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "argusai"
    core_dir = package / "core"
    runners = package / "vendor" / "runners"
    core_dir.mkdir(parents=True)
    runners.mkdir(parents=True)
    (runners / "package-lock.json").write_text("{}", encoding="utf-8")

    fake_module = core_dir / "managed_runtimes.py"
    fake_module.write_text("# stub\n", encoding="utf-8")
    monkeypatch.setattr(mr, "__file__", str(fake_module))
    monkeypatch.setattr(mr.sys, "platform", "darwin")

    assert mr.vendor_assets_dir() == package / "vendor"
    assert mr.node_tools_lockfile().is_file()

