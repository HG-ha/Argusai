"""Update installation must never discard live work or close on a failed handoff."""
from types import SimpleNamespace
from threading import Lock
from unittest.mock import Mock
import hashlib
import httpx

import pytest

from argusai.gui.updates import UpdatesMixin


class Host(UpdatesMixin):
    def __init__(self):
        self._window = object()
        self._update_install_lock = Lock()
        self._assistant_sessions = {}
        self.tasks = SimpleNamespace(list=lambda: [])
        self.proxy = SimpleNamespace(is_running=False)
        self.reverse_jobs = SimpleNamespace(list=lambda: [])
        self.quit_application = Mock(return_value={"ok": True})
        self.manager = SimpleNamespace(
            status=lambda: {"status": "downloaded", "version": "0.1.4"},
            prepare_install=Mock(return_value={"ok": True, "command": ["update-helper"], "cwd": None}),
            installation_aborted=Mock(),
        )

    def _updates(self):
        return self.manager

    def _update_protected_paths(self):
        return getattr(self, "protected_paths", ())


@pytest.mark.parametrize("busy", ["agent", "acp", "scan", "proxy", "reverse", "background", "team", "migration"])
def test_install_preserves_running_work(busy):
    host = Host()
    if busy == "agent":
        host._assistant_running_sessions = {"live"}
    elif busy == "acp":
        host._active_acp_turns = {"live": object()}
    elif busy == "scan":
        host.tasks = SimpleNamespace(list=lambda: [{"id": "scan"}], get=lambda _: SimpleNamespace(is_running=lambda: True))
    elif busy == "proxy":
        host.proxy.is_running = True
    elif busy == "reverse":
        host.reverse_jobs = SimpleNamespace(list=lambda: [{"active_runtime_sessions": 1}])
    elif busy == "background":
        host._assistant_sessions = {"s": SimpleNamespace(_background_jobs=SimpleNamespace(running_count=lambda: 1))}
    elif busy == "team":
        host._assistant_sessions = {"s": SimpleNamespace(_agent_thread_manager=SimpleNamespace(active_agent_count=lambda: 1))}
    else:
        host._data_root_migration = {"status": "running"}
    result = host.install_update()
    assert result["error_code"] == "busy"
    assert result["status"] == "downloaded"
    host.manager.prepare_install.assert_not_called()
    host.quit_application.assert_not_called()
    assert host._update_install_lock.acquire(blocking=False)


def test_no_desktop_window_cannot_install():
    host = Host()
    host._window = None
    assert host.install_update()["error_code"] == "no_window"
    host.quit_application.assert_not_called()


def test_staging_failure_does_not_quit():
    host = Host()
    host.manager.prepare_install.return_value = {"ok": False, "error": "Checksum mismatch"}
    assert host.install_update()["ok"] is False
    host.quit_application.assert_not_called()


def test_task_starting_during_staging_defers_install(monkeypatch):
    host = Host()
    def prepare():
        host._assistant_running_sessions = {"new"}
        return {"ok": True, "command": ["helper"]}
    host.manager.prepare_install.side_effect = prepare
    spawn = Mock()
    monkeypatch.setattr("argusai.gui.updates.subprocess.Popen", spawn)
    assert host.install_update()["error_code"] == "busy"
    spawn.assert_not_called()
    host.quit_application.assert_not_called()


@pytest.mark.parametrize("scenario", ["success", "helper_exited", "quit_failed", "late_work"])
def test_handoff_waits_for_helper_and_real_exit(monkeypatch, scenario):
    host = Host()
    callbacks = []
    timer = SimpleNamespace(start=lambda: None)
    def make_timer(delay, callback):
        callbacks.append(callback)
        return timer
    process = Mock()
    process.poll.return_value = 1 if scenario == "helper_exited" else None
    monkeypatch.setattr("argusai.gui.updates.threading.Timer", make_timer)
    spawn = Mock(return_value=process)
    monkeypatch.setattr("argusai.gui.updates.subprocess.Popen", spawn)
    monkeypatch.setenv("NUITKA_ONEFILE_PARENT", "old-parent")
    monkeypatch.setenv("NUITKA_ONEFILE_BINARY", "old-binary")
    if scenario == "quit_failed":
        host.quit_application.return_value = {"ok": False}
    assert host.install_update()["status"] == "installing"
    assert "NUITKA_ONEFILE_PARENT" not in spawn.call_args.kwargs["env"]
    assert "NUITKA_ONEFILE_BINARY" not in spawn.call_args.kwargs["env"]
    assert host.update_status()["status"] == "installing"
    assert host.install_update()["error_code"] == "install_pending"
    host.quit_application.assert_not_called()
    if scenario == "late_work":
        host._assistant_running_sessions = {"new"}
    callbacks[0]()
    if scenario == "success":
        host.quit_application.assert_called_once()
        process.terminate.assert_not_called()
    else:
        process.terminate.assert_called_once()
        assert host.update_status()["ok"] is False
        if scenario != "quit_failed":
            host.quit_application.assert_not_called()
    assert host._update_install_lock.acquire(blocking=False)


def test_spawn_failure_keeps_application_open(monkeypatch):
    host = Host()
    monkeypatch.setattr("argusai.gui.updates.subprocess.Popen", Mock(side_effect=OSError("cannot launch")))
    assert host.install_update()["error_code"] == "install_failed"
    host.quit_application.assert_not_called()


def test_handoff_schedule_failure_stops_helper_and_allows_retry(monkeypatch):
    host = Host()
    process = Mock()
    monkeypatch.setattr("argusai.gui.updates.subprocess.Popen", Mock(return_value=process))
    timer = SimpleNamespace(start=Mock(side_effect=RuntimeError("cannot schedule")))
    monkeypatch.setattr("argusai.gui.updates.threading.Timer", lambda *_: timer)
    assert host.install_update()["error_code"] == "install_failed"
    process.terminate.assert_called_once()
    assert not host._update_handoff_pending
    assert host._update_install_lock.acquire(blocking=False)
    host.quit_application.assert_not_called()


def test_install_refreshes_data_protection_after_settings_change():
    host = Host()
    host.manager.protected_paths = ("old-external-data",)
    host.protected_paths = ("new-data-inside-install",)
    def prepare():
        assert host.manager.protected_paths == host.protected_paths
        return {"ok": False, "error": "Move application data outside the installation directory"}
    host.manager.prepare_install.side_effect = prepare
    assert host.install_update()["ok"] is False
    host.quit_application.assert_not_called()


def test_real_manager_handoff_failure_restores_ready_state(tmp_path, monkeypatch):
    from argusai.core.updater import MANIFEST_URL, UpdateManager

    target = tmp_path / "installed"
    target.mkdir()
    (target / "licenses").mkdir()
    for name in ("ArgusAI.exe", "Uninstall.exe", "launch-gui.vbs", "licenses/LICENSE"):
        (target / name).write_bytes(b"old installation fixture")
    package = b"verified installer fixture"
    manifest = {
        "schema_version": 1, "version": "0.1.4", "notes": "Update",
        "release_url": "https://github.com/HG-ha/Argusai/releases/tag/v0.1.4",
        "assets": {"win-x64": {
            "url": "https://github.com/HG-ha/Argusai/releases/download/v0.1.4/ArgusAI-0.1.4-win-x64-setup.exe",
            "size": len(package), "sha256": hashlib.sha256(package).hexdigest(),
        }},
    }
    def route(request):
        return httpx.Response(200, json=manifest) if str(request.url) == MANIFEST_URL else httpx.Response(200, content=package)
    with httpx.Client(transport=httpx.MockTransport(route)) as client:
        manager = UpdateManager(tmp_path / "cache", "0.1.3", client=client,
                                platform_key="win-x64", packaged=True, executable=target / "ArgusAI.exe")
        assert manager.check()["status"] == "available"
        manager.start_download()
        manager._worker.join(3)
        assert manager.status()["status"] == "downloaded"
        host = Host()
        host.manager = manager
        monkeypatch.setattr("argusai.gui.updates.subprocess.Popen", Mock(side_effect=OSError("blocked helper")))
        assert host.install_update()["error_code"] == "install_failed"
        assert manager.status()["status"] == "downloaded"
        # The next attempt reaches preparation, rather than getting stuck installing.
        assert host.install_update()["error_code"] == "install_failed"
        assert manager.status()["status"] == "downloaded"
        host.quit_application.assert_not_called()
