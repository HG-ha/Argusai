"""Unit tests for optional OS service registration helpers."""

from __future__ import annotations

import sys
from pathlib import Path

from typer.testing import CliRunner

from argusai.cli.main import _register_commands, app
from argusai.cli.service_install import (
    HOST_SERVICE,
    _linux_unit_body,
    host_service_spec,
    launch_command,
    service_plan,
)

_register_commands()
runner = CliRunner()


def test_launch_command_uses_serve_run() -> None:
    cmd = launch_command(cli_args=HOST_SERVICE.cli_args, boot_policy="startup")
    assert cmd[-2:] == ["serve", "run"]
    name = Path(cmd[0]).name.lower()
    # Windows prefers pythonw (no console flash); else console script / python.
    assert name in {
        "argusai.exe",
        "argusai",
        "python.exe",
        "pythonw.exe",
        Path(sys.executable).name.lower(),
    }
    if name.startswith("python"):
        assert cmd[1:3] == ["-m", "argusai"]


def test_windows_logon_full_host_launches_tray_gui() -> None:
    cmd = launch_command(cli_args=HOST_SERVICE.cli_args, boot_policy="logon")
    if sys.platform in {"win32", "darwin"}:
        assert "--tray" in cmd
        assert "--gui" in cmd
        assert "serve" not in cmd
    else:
        assert cmd[-2:] == ["serve", "run"]
    cron = launch_command(
        cli_args=host_service_spec(cron_only=True).cli_args,
        boot_policy="logon",
    )
    assert "serve" in cron and "run" in cron and "--cron-only" in cron


def test_windows_task_xml_is_hidden() -> None:
    from argusai.cli.service_install import _windows_task_xml

    xml = _windows_task_xml(
        HOST_SERVICE,
        ["C:\\Python\\pythonw.exe", "-m", "argusai", "serve", "run"],
        boot_policy="logon",
    )
    assert "<Hidden>true</Hidden>" in xml
    assert "LogonTrigger" in xml
    assert "pythonw.exe" in xml
    assert "serve" in xml
    assert "<Author>ArgusAI</Author>" in xml
    assert "<RestartOnFailure>" in xml


def test_windows_startup_task_uses_s4u_principal() -> None:
    """BootTrigger + InteractiveToken would wait for a logon that has not happened."""
    from argusai.cli.service_install import _windows_task_xml

    xml = _windows_task_xml(
        HOST_SERVICE,
        ["C:\\Apps\\ArgusAI.exe", "serve", "run"],
        boot_policy="startup",
    )
    assert "BootTrigger" in xml
    assert "<LogonType>S4U</LogonType>" in xml
    assert "<Priority>4</Priority>" in xml

    fallback = _windows_task_xml(
        HOST_SERVICE,
        ["C:\\Apps\\ArgusAI.exe", "serve", "run"],
        boot_policy="startup",
        logon_type="InteractiveToken",
    )
    assert "<LogonType>InteractiveToken</LogonType>" in fallback


def test_windows_status_value_matching() -> None:
    from argusai.cli.service_install import _win_status_is_active

    assert _win_status_is_active("Running") is True
    assert _win_status_is_active("正在运行") is True
    assert _win_status_is_active("Ready") is False
    assert _win_status_is_active("就绪") is False
    assert _win_status_is_active("Not Running") is False
    assert _win_status_is_active("") is False


def test_cron_only_spec() -> None:
    spec = host_service_spec(cron_only=True)
    assert spec.cli_args == ("serve", "run", "--cron-only")


def test_linux_unit_body_quotes_spaces(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Python 3.12" / "python"))
    body = _linux_unit_body(
        HOST_SERVICE,
        launch_command(cli_args=HOST_SERVICE.cli_args, boot_policy="startup"),
    )
    assert "ExecStart=" in body
    assert "serve" in body
    assert "run" in body


def test_service_plan_shape() -> None:
    plan = service_plan(HOST_SERVICE)
    assert plan.get("command")
    if sys.platform == "win32" and plan.get("boot_policy") == "logon":
        assert plan["kind"] == "run_key"
        assert "--tray" in plan["command"]
    elif sys.platform == "darwin" and plan.get("boot_policy") == "logon":
        assert "--tray" in plan["command"]
    else:
        assert plan["command"][-2:] == ["serve", "run"]


def test_serve_install_dry_run() -> None:
    result = runner.invoke(app, ["serve", "install", "--dry-run", "--json"])
    assert result.exit_code == 0
    if sys.platform in {"win32", "darwin"}:
        assert "--tray" in result.stdout
    else:
        assert "serve" in result.stdout
        assert "run" in result.stdout


def test_serve_group_help() -> None:
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    for name in ("install", "boot", "uninstall", "start", "stop", "status", "restart", "run"):
        assert name in result.stdout


def test_serve_boot_requires_install() -> None:
    result = runner.invoke(app, ["serve", "boot", "logon"])
    # Either not installed, or platform accepted — must not crash.
    assert result.exit_code in {0, 1}
    if result.exit_code != 0:
        assert "not installed" in (result.stdout + result.stderr).lower() or "install" in (
            result.stdout + result.stderr
        ).lower()


def test_serve_status_not_installed_ok() -> None:
    result = runner.invoke(app, ["serve", "status", "--json"])
    assert result.exit_code == 0
    assert "installed" in result.stdout


def test_dev_runtime_warns_about_autostart() -> None:
    from argusai.cli.service_install import (
        is_packaged_runtime,
        launch_environment_warnings,
    )

    # Under pytest we almost always run from a venv / source tree.
    if not is_packaged_runtime():
        warns = launch_environment_warnings()
        assert warns
        assert any("Source/dev" in w or "venv" in w.lower() for w in warns)


def test_boot_policy_normalize() -> None:
    from argusai.cli.service_install import normalize_boot_policy

    if sys.platform == "win32":
        assert normalize_boot_policy("startup") == "startup"
        assert normalize_boot_policy("bogus") == "logon"
    elif sys.platform.startswith("linux"):
        assert normalize_boot_policy("linger") == "linger"
        assert normalize_boot_policy("bogus") == "linger"


def test_macos_plist_body_is_launch_agent() -> None:
    from argusai.cli.service_install import _macos_label, _macos_plist_body

    cmd = ["/Applications/ArgusAI.app/Contents/MacOS/ArgusAI", "serve", "run"]
    body = _macos_plist_body(HOST_SERVICE, cmd)
    assert "<string>com.argusai.host</string>" in body
    assert "<string>serve</string>" in body
    assert "<string>run</string>" in body
    assert "<key>AssociatedBundleIdentifiers</key>" in body
    assert "<string>com.argusai.app</string>" in body
    assert "<string>Aqua</string>" in body
    assert "<string>Background</string>" in body
    gui_body = _macos_plist_body(
        HOST_SERVICE,
        ["/Applications/ArgusAI.app/Contents/MacOS/ArgusAI", "--gui", "--tray"],
    )
    assert "<string>Interactive</string>" in gui_body
    assert _macos_label(HOST_SERVICE) == "com.argusai.host"
    assert _macos_label(host_service_spec(cron_only=True)) == "com.argusai.host-cron"
    spaced = _macos_plist_body(HOST_SERVICE, ["/opt/Argus AI/ArgusAI", "serve", "run"])
    assert "<string>/opt/Argus AI/ArgusAI</string>" in spaced


def test_macos_service_plan(monkeypatch) -> None:
    from argusai.cli import service_install as si

    monkeypatch.setattr(si.sys, "platform", "darwin")
    monkeypatch.setattr(
        "argusai.cli.macos_login_item.available_for_host",
        lambda cron_only=False: False,
    )
    plan = si.service_plan(HOST_SERVICE)
    assert plan["ok"] is True
    assert plan["kind"] == "launch_agent"
    assert plan["label"] == "com.argusai.host"
    assert "RunAtLoad" in plan["plist_body"]
    assert si.normalize_boot_policy("bogus") == "logon"
    ids = {item["id"] for item in si.boot_policy_choices()}
    assert ids == {"logon"}


def test_macos_install_bootstraps_launch_agent(tmp_path, monkeypatch) -> None:
    from argusai.cli import service_install as si

    monkeypatch.setattr(si.sys, "platform", "darwin")
    monkeypatch.setattr(
        "argusai.cli.macos_login_item.available_for_host",
        lambda cron_only=False: False,
    )
    plist = tmp_path / "LaunchAgents" / "com.argusai.host.plist"
    monkeypatch.setattr(si, "_macos_plist_path", lambda spec: plist)
    monkeypatch.setattr(si, "_macos_log_dir", lambda: tmp_path / "Logs")
    monkeypatch.setattr(si, "_macos_uid", lambda: 501)
    monkeypatch.setattr(si, "save_host_service_prefs", lambda payload: None)
    monkeypatch.setattr(si, "launch_environment_warnings", lambda: [])
    calls: list[list[str]] = []

    class _Proc:
        def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(argv, *, check=True):
        calls.append(list(argv))
        if argv[:2] == ["launchctl", "print"]:
            return _Proc(0, "state = running\npid = 4242\n")
        return _Proc(0)

    monkeypatch.setattr(si, "_run", fake_run)
    receipt = si.install_service(HOST_SERVICE, start=True, boot_policy="logon")
    assert receipt["ok"] is True
    assert receipt["kind"] == "launch_agent"
    assert receipt["started"] is True
    assert plist.is_file()
    text = plist.read_text(encoding="utf-8")
    assert "com.argusai.host" in text
    assert any(cmd[:2] == ["launchctl", "bootstrap"] for cmd in calls)
    assert any(cmd[:2] == ["launchctl", "kickstart"] for cmd in calls)

    started = si.start_service(HOST_SERVICE)
    assert started["ok"] is True
    status = si._status_macos(HOST_SERVICE)
    assert status["installed"] is True
    assert status["active"] is True
    stopped = si.stop_service(HOST_SERVICE)
    assert stopped["ok"] is True
    assert stopped["was_running"] is True


def test_macos_install_uses_login_item_when_available(tmp_path, monkeypatch) -> None:
    from argusai.cli import macos_login_item as mli
    from argusai.cli import service_install as si

    monkeypatch.setattr(si.sys, "platform", "darwin")
    monkeypatch.setattr(mli, "available_for_host", lambda cron_only=False: not cron_only)
    monkeypatch.setattr(
        mli,
        "register_main_app",
        lambda: {
            "ok": True,
            "status": "enabled",
            "registered": True,
            "enabled": True,
            "requires_approval": False,
            "bundle": "/Applications/ArgusAI.app",
        },
    )
    monkeypatch.setattr(
        mli,
        "open_app_hidden",
        lambda bundle=None: {"ok": True, "bundle": "/Applications/ArgusAI.app"},
    )
    monkeypatch.setattr(mli, "unregister_main_app", lambda: {"ok": True, "removed": True})
    monkeypatch.setattr(
        mli,
        "login_item_status",
        lambda: {
            "ok": True,
            "available": True,
            "status": "enabled",
            "registered": True,
            "enabled": True,
            "requires_approval": False,
            "bundle": "/Applications/ArgusAI.app",
        },
    )
    plist = tmp_path / "LaunchAgents" / "com.argusai.host.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("stale", encoding="utf-8")
    monkeypatch.setattr(si, "_macos_plist_path", lambda spec: plist)
    monkeypatch.setattr(si, "_macos_bootout", lambda spec: None)
    monkeypatch.setattr(si, "_macos_uid", lambda: 501)
    monkeypatch.setattr(si, "_macos_is_loaded", lambda spec: (False, ""))
    monkeypatch.setattr(si, "save_host_service_prefs", lambda payload: None)
    monkeypatch.setattr(si, "launch_environment_warnings", lambda: [])

    receipt = si.install_service(HOST_SERVICE, start=True, boot_policy="logon")
    assert receipt["kind"] == "login_item"
    assert receipt["started"] is True
    assert not plist.exists()

    plan = si.service_plan(HOST_SERVICE)
    assert plan["kind"] == "login_item"
    status = si._status_macos(HOST_SERVICE)
    assert status["kind"] == "login_item"
    assert status["installed"] is True
    assert status["active"] is True

    removed = si.uninstall_service(HOST_SERVICE)
    assert removed["removed"] is True


def test_macos_status_parses_launchctl_list() -> None:
    from argusai.cli.service_install import _macos_text_is_active

    assert _macos_text_is_active("state = running\npid = 12\n") is True
    assert _macos_text_is_active("state = waiting\n") is False
    assert _macos_text_is_active("PID\tStatus\tLabel\n4321\t0\tcom.argusai.host\n") is True
    assert _macos_text_is_active("-\t0\tcom.argusai.host\n") is False
    assert _macos_text_is_active("") is False


def test_windows_logon_install_writes_run_key(monkeypatch) -> None:
    from argusai.cli import service_install as si
    from argusai.cli import windows_run_key as wrk

    monkeypatch.setattr(si.sys, "platform", "win32")
    written: dict[str, str] = {}

    def fake_write(command: str) -> dict:
        written["command"] = command
        return {"ok": True}

    monkeypatch.setattr(wrk, "write_run_key", fake_write)
    monkeypatch.setattr(wrk, "start_run_key_command", lambda cmd: {"ok": True})
    monkeypatch.setattr(wrk, "delete_run_key", lambda: {"ok": True, "removed": False})
    monkeypatch.setattr(si, "_delete_windows_task", lambda spec: False)
    monkeypatch.setattr(si, "save_host_service_prefs", lambda payload: None)
    monkeypatch.setattr(si, "launch_environment_warnings", lambda: [])
    receipt = si.install_service(si.HOST_SERVICE, start=True, boot_policy="logon")
    assert receipt["kind"] == "run_key"
    assert "--tray" in written["command"]


def test_privilege_probe_marks_startup_without_admin(monkeypatch) -> None:
    from argusai.cli import service_install as si

    monkeypatch.setattr(si.sys, "platform", "win32")
    monkeypatch.setattr(si, "is_process_elevated", lambda: False)
    probe = si.privilege_probe(boot_policy="startup")
    assert probe["needs_elevation"] is True
    assert probe["elevated"] is False
    logon = si.privilege_probe(boot_policy="logon")
    assert logon["needs_elevation"] is False


def test_startup_install_refuses_without_admin(monkeypatch) -> None:
    from argusai.cli import service_install as si

    monkeypatch.setattr(si.sys, "platform", "win32")
    monkeypatch.setattr(si, "is_process_elevated", lambda: False)

    def _no_schtasks(*_args, **_kwargs):
        raise AssertionError("schtasks should not run before the privilege check")

    monkeypatch.setattr(si, "_run", _no_schtasks)
    try:
        si.install_service(si.HOST_SERVICE, start=False, boot_policy="startup")
    except si.HostServiceError as exc:
        assert exc.code == "serve_err_startup_needs_admin"
    else:
        raise AssertionError("expected HostServiceError")


def test_classify_host_service_error() -> None:
    from argusai.cli.service_install import classify_host_service_error

    assert classify_host_service_error("Access is denied.") == "serve_err_access_denied_logon"
    assert classify_host_service_error("拒绝访问", boot_policy="startup") == "serve_err_startup_needs_admin"
    assert classify_host_service_error("schtasks delete failed: 拒绝访问") == (
        "serve_err_access_denied_delete"
    )


def test_host_service_gui_status_shape() -> None:
    from argusai.gui.host_service import HostServiceMixin

    class _Api(HostServiceMixin):
        def _channels_runtime(self):
            class R:
                _host_lock_file = None
                _adapters = {}

            return R()

    status = _Api().get_host_service_status()
    assert status.get("ok") is True
    assert "boot_policies" in status
    assert "platform" in status
    assert "elevated" in status
    assert "needs_elevation" in status
