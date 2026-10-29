"""Optional OS service registration for the headless host.

Explicit install only — never auto-registers on first run. Supports:
- Windows: HKCU Run (logon tray) or Scheduled Task (``startup`` / cron-only)
- Linux: systemd --user unit — ``user`` or ``linger`` (boot without interactive login)
- macOS: Login Item (``SMAppService``, packaged ``.app``, macOS 13+) or a
  user LaunchAgent fallback (dev / older macOS / cron-only)
"""

from __future__ import annotations

import json
import locale
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ServiceSpec:
    """One installable host process."""

    unit: str
    display_name: str
    description: str
    cli_args: tuple[str, ...]  # after ``python -m argusai``


HOST_SERVICE = ServiceSpec(
    unit="argusai-host",
    display_name="ArgusAI Host",
    description="ArgusAI headless host: channel adapters + cron scheduler.",
    cli_args=("serve", "run"),
)

HOST_CRON_ONLY_SERVICE = ServiceSpec(
    unit="argusai-host-cron",
    display_name="ArgusAI Host (cron only)",
    description="ArgusAI headless host: cron scheduler only.",
    cli_args=("serve", "run", "--cron-only"),
)

LEGACY_SERVICES: tuple[ServiceSpec, ...] = (
    ServiceSpec(
        unit="argusai-cron",
        display_name="ArgusAI Cron",
        description="legacy",
        cli_args=("cron", "serve"),
    ),
    ServiceSpec(
        unit="argusai-channels",
        display_name="ArgusAI Channels",
        description="legacy",
        cli_args=("channels", "serve"),
    ),
)


class HostServiceError(RuntimeError):
    """Privilege / registration failure with a stable ``code`` for the GUI."""

    def __init__(self, code: str, message: str = "") -> None:
        from argusai.cli.locale import label

        self.code = str(code or "").strip() or "serve_err_unknown"
        super().__init__(message or label(self.code))


def is_process_elevated() -> bool:
    """True when this process has a Windows admin token."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def privilege_probe(*, boot_policy: str | None = None) -> dict[str, Any]:
    """Tell the UI whether the chosen boot policy needs elevation."""
    from argusai.cli.locale import label

    policy = normalize_boot_policy(boot_policy)
    elevated = is_process_elevated()
    needs = sys.platform == "win32" and policy == "startup" and not elevated
    return {
        "elevated": elevated,
        "needs_elevation": needs,
        "boot_policy": policy,
        "hint": label("serve_err_startup_needs_admin") if needs else "",
    }


def classify_host_service_error(text: str, *, boot_policy: str = "") -> str:
    raw = str(text or "")
    if "serve_err_" in raw:
        for code in (
            "serve_err_startup_needs_admin",
            "serve_err_access_denied_delete",
            "serve_err_access_denied_logon",
            "serve_err_macos_login_item",
            "serve_err_unknown",
        ):
            if code in raw:
                return code
    if _win_access_denied(raw):
        if str(boot_policy or "").strip().lower() == "startup":
            return "serve_err_startup_needs_admin"
        if "delete" in raw.lower() or "删除" in raw:
            return "serve_err_access_denied_delete"
        return "serve_err_access_denied_logon"
    return ""


def host_service_spec(*, cron_only: bool = False) -> ServiceSpec:
    return HOST_CRON_ONLY_SERVICE if cron_only else HOST_SERVICE


def host_service_prefs_path() -> Path:
    from argusai.core.paths import PathManager

    return PathManager().app_root / "host_service.json"


def load_host_service_prefs() -> dict[str, Any]:
    path = host_service_prefs_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_host_service_prefs(payload: dict[str, Any]) -> None:
    path = host_service_prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current = load_host_service_prefs()
    current.update(dict(payload or {}))
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def boot_policy_choices() -> list[dict[str, str]]:
    from argusai.cli.locale import label

    if sys.platform == "win32":
        return [
            {
                "id": "logon",
                "label": label("boot_policy_logon"),
                "hint": label("boot_policy_logon_hint"),
            },
            {
                "id": "startup",
                "label": label("boot_policy_startup"),
                "hint": label("boot_policy_startup_hint"),
                "needs_admin": True,
            },
        ]
    if sys.platform.startswith("linux"):
        return [
            {
                "id": "user",
                "label": label("boot_policy_user"),
                "hint": label("boot_policy_user_hint"),
            },
            {
                "id": "linger",
                "label": label("boot_policy_linger"),
                "hint": label("boot_policy_linger_hint"),
            },
        ]
    if sys.platform == "darwin":
        return [
            {
                "id": "logon",
                "label": label("boot_policy_logon"),
                "hint": label("boot_policy_macos_logon_hint"),
            }
        ]
    return [{"id": "logon", "label": label("boot_policy_logon"), "hint": ""}]


def normalize_boot_policy(value: str | None) -> str:
    text = str(value or "").strip().lower()
    allowed = {item["id"] for item in boot_policy_choices()}
    if text in allowed:
        return text
    if sys.platform == "win32":
        return "logon"
    if sys.platform.startswith("linux"):
        return "linger"
    return "logon"


def is_packaged_runtime() -> bool:
    """True when running a frozen / Nuitka launcher (not a bare venv / pip script)."""
    try:
        from argusai.core.infra.runtime_exec import is_frozen_runtime

        return bool(is_frozen_runtime())
    except Exception:
        return bool(getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None))


def launch_environment_warnings() -> list[str]:
    """Human-readable caveats for OS-service registration from this runtime."""
    warnings: list[str] = []
    exe = Path(sys.executable).resolve()
    if is_packaged_runtime():
        return warnings
    warnings.append(
        "Source/dev runtime: the service will call this Python/venv. "
        "Moving the repo, recreating .venv, or ephemeral `uv run` envs can break autostart. "
        "Packaged builds (or a stable `uv tool install` / pip install) are recommended for boot."
    )
    if any(part.lower() in {".venv", "venv"} for part in exe.parts):
        warnings.append(f"Interpreter is inside a virtualenv: {exe}")
    if sys.platform == "win32":
        warnings.append(
            "Windows “startup” (pre-logon) is especially fragile with a project venv; "
            "prefer packaged install, or use “logon” while developing."
        )
    return warnings


def launch_command(
    *,
    cli_args: tuple[str, ...] | list[str],
    boot_policy: str | None = None,
) -> list[str]:
    """Stable argv for services.

    Packaged/frozen builds invoke the binary directly. Dev installs on Windows
    prefer ``pythonw.exe -m argusai`` so Task Scheduler does not flash a console;
    elsewhere prefer the ``argusai`` console script, else ``python -m argusai``.

    Windows / macOS logon + full host starts the desktop GUI hidden
    (tray on Windows, Dock on macOS) so the user can reopen the window
    without a second process fighting for the channel lock.
    Pre-logon ``startup`` and cron-only units stay on ``serve run``.
    """
    args = [str(a) for a in cli_args]
    policy = normalize_boot_policy(boot_policy)
    if (
        sys.platform in {"win32", "darwin"}
        and args == ["serve", "run"]
        and policy == "logon"
    ):
        args = ["--gui", "--tray"]
    if is_packaged_runtime():
        from argusai.core.infra.runtime_exec import frozen_self_executable

        return [frozen_self_executable(), *args]
    exe = Path(sys.executable).resolve()
    if sys.platform == "win32":
        # Console ``argusai.exe`` / ``python.exe`` under a scheduled task open a
        # visible cmd window — avoid that for headless host registration.
        pythonw = exe.with_name("pythonw.exe")
        if pythonw.is_file():
            return [str(pythonw), "-m", "argusai", *args]
    scripts_dir = exe.parent
    for name in ("argusai.exe", "argusai"):
        candidate = scripts_dir / name
        if candidate.is_file():
            return [str(candidate), *args]
    return [str(exe), "-m", "argusai", *args]


def launch_command_line(
    *,
    cli_args: tuple[str, ...] | list[str],
    boot_policy: str | None = None,
) -> str:
    return subprocess.list2cmdline(
        launch_command(cli_args=cli_args, boot_policy=boot_policy)
    )


def _win_task_name(spec: ServiceSpec) -> str:
    return f"ArgusAI\\{spec.unit}"


def _linux_unit_path(spec: ServiceSpec) -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "systemd" / "user" / f"{spec.unit}.service"


def _linux_unit_body(spec: ServiceSpec, cmd: list[str]) -> str:
    exec_line = " ".join(shlex.quote(part) for part in cmd)
    return (
        "[Unit]\n"
        f"Description={spec.description}\n"
        "After=default.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_line}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


_MACOS_LABELS = {
    "argusai-host": "com.argusai.host",
    "argusai-host-cron": "com.argusai.host-cron",
    "argusai-cron": "com.argusai.cron",
    "argusai-channels": "com.argusai.channels",
}


def _macos_label(spec: ServiceSpec) -> str:
    return _MACOS_LABELS.get(spec.unit, f"com.argusai.{spec.unit}")


def _macos_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return 0
    return int(getuid())


def _macos_gui_domain() -> str:
    return f"gui/{_macos_uid()}"


def _macos_service_target(spec: ServiceSpec) -> str:
    return f"{_macos_gui_domain()}/{_macos_label(spec)}"


def _macos_plist_path(spec: ServiceSpec) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_macos_label(spec)}.plist"


def _macos_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "ArgusAI"


def _macos_plist_body(spec: ServiceSpec, cmd: list[str]) -> str:
    from argusai.cli.macos_login_item import APP_BUNDLE_ID

    label = _macos_label(spec)
    args_xml = "\n".join(f"    <string>{_xml_escape(part)}</string>" for part in cmd)
    log_dir = _macos_log_dir()
    stdout = _xml_escape(str(log_dir / f"{spec.unit}.out.log"))
    stderr = _xml_escape(str(log_dir / f"{spec.unit}.err.log"))
    workdir_src = Path(cmd[0]).expanduser() if cmd else Path.home()
    try:
        workdir = str(workdir_src.resolve().parent if cmd else Path.home())
    except OSError:
        workdir = str(workdir_src.parent if cmd else Path.home())
    gui = "--gui" in cmd
    process_type = "Interactive" if gui else "Background"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{_xml_escape(label)}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{args_xml}\n"
        "  </array>\n"
        "  <key>WorkingDirectory</key>\n"
        f"  <string>{_xml_escape(workdir)}</string>\n"
        "  <key>AssociatedBundleIdentifiers</key>\n"
        "  <array>\n"
        f"    <string>{_xml_escape(APP_BUNDLE_ID)}</string>\n"
        "  </array>\n"
        "  <key>LimitLoadToSessionType</key>\n"
        "  <string>Aqua</string>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <dict>\n"
        "    <key>SuccessfulExit</key>\n"
        "    <false/>\n"
        "  </dict>\n"
        "  <key>ThrottleInterval</key>\n"
        "  <integer>5</integer>\n"
        "  <key>ProcessType</key>\n"
        f"  <string>{process_type}</string>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{stdout}</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{stderr}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _macos_already_loaded(text: str) -> bool:
    lowered = (text or "").lower()
    return "already" in lowered and (
        "bootstrapped" in lowered or "loaded" in lowered or "in use" in lowered
    )


def _macos_bootstrap(spec: ServiceSpec, plist: Path) -> subprocess.CompletedProcess[str]:
    proc = _run(["launchctl", "bootstrap", _macos_gui_domain(), str(plist)], check=False)
    if proc.returncode == 0 or _macos_already_loaded(proc.stderr or proc.stdout or ""):
        return proc
    legacy = _run(["launchctl", "load", "-w", str(plist)], check=False)
    return legacy if legacy.returncode == 0 else proc


def _macos_kickstart(spec: ServiceSpec) -> subprocess.CompletedProcess[str]:
    proc = _run(
        ["launchctl", "kickstart", "-k", _macos_service_target(spec)],
        check=False,
    )
    if proc.returncode == 0:
        return proc
    return _run(["launchctl", "start", _macos_label(spec)], check=False)


def _macos_bootout(spec: ServiceSpec) -> None:
    _run(["launchctl", "bootout", _macos_service_target(spec)], check=False)
    _run(["launchctl", "unload", str(_macos_plist_path(spec))], check=False)
    _run(["launchctl", "stop", _macos_label(spec)], check=False)


def _macos_is_loaded(spec: ServiceSpec) -> tuple[bool, str]:
    printed = _run(["launchctl", "print", _macos_service_target(spec)], check=False)
    if printed.returncode == 0:
        return True, printed.stdout or ""
    listed = _run(["launchctl", "list", _macos_label(spec)], check=False)
    text = (listed.stdout or "").strip()
    if listed.returncode == 0 and text:
        return True, text
    return False, ""


def _macos_text_is_active(text: str) -> bool:
    for line in (text or "").splitlines():
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered.startswith("state =") and "running" in lowered:
            return True
        if lowered.startswith("pid ="):
            pid = stripped.split("=", 1)[1].strip()
            if pid.isdigit() and int(pid) > 0:
                return True
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    row = lines[-1] if lines[0].lower().startswith("pid") else lines[0]
    parts = row.split()
    return len(parts) >= 3 and parts[0] not in {"-", "PID"} and parts[0].isdigit()


def _xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _windows_task_xml(
    spec: ServiceSpec,
    cmd: list[str],
    *,
    boot_policy: str,
    logon_type: str = "",
) -> str:
    """Task Scheduler XML with Hidden=true so host start does not flash a console."""
    command = _xml_escape(cmd[0])
    arguments = _xml_escape(subprocess.list2cmdline(cmd[1:])) if len(cmd) > 1 else ""
    workdir = _xml_escape(str(Path(cmd[0]).resolve().parent))
    description = _xml_escape(spec.description)
    task_uri = _xml_escape(f"\\ArgusAI\\{spec.unit}")
    sid = _xml_escape(_windows_user_sid())
    user_xml = f"<UserId>{sid}</UserId>\n      " if sid else ""
    if boot_policy == "startup":
        trigger = "<BootTrigger><Enabled>true</Enabled></BootTrigger>"
        run_level = "HighestAvailable"
        # BootTrigger fires before any interactive session exists, so
        # InteractiveToken would defer the task until logon. S4U is the
        # password-less "run whether the user is logged on or not" principal.
        default_logon = "S4U"
        priority = 4
    else:
        trigger = "<LogonTrigger><Enabled>true</Enabled></LogonTrigger>"
        run_level = "LeastPrivilege"
        default_logon = "InteractiveToken"
        priority = 7
    logon = _xml_escape(logon_type or default_logon)
    args_xml = f"<Arguments>{arguments}</Arguments>" if arguments else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>ArgusAI</Author>
    <Description>{description}</Description>
    <URI>{task_uri}</URI>
  </RegistrationInfo>
  <Triggers>
    {trigger}
  </Triggers>
  <Principals>
    <Principal id="Author">
      {user_xml}<LogonType>{logon}</LogonType>
      <RunLevel>{run_level}</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>{priority}</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      {args_xml}
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _windows_user_sid() -> str:
    """Current user SID for Task Scheduler principals (empty if unavailable)."""
    if sys.platform != "win32":
        return ""
    proc = _run(["whoami", "/user", "/fo", "csv", "/nh"], check=False)
    if proc.returncode != 0:
        return ""
    text = (proc.stdout or "").strip().strip('"')
    if "," not in text:
        return ""
    sid = text.rsplit(",", 1)[-1].strip().strip('"')
    return sid if sid.startswith("S-1-") else ""


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a helper process without flashing a console window on Windows GUI hosts."""
    # Windows ``schtasks`` prints localized status in the OEM/ANSI code page;
    # forcing UTF-8 turns 「正在运行」 into mojibake and breaks active detection.
    encoding = "utf-8"
    if sys.platform == "win32":
        encoding = locale.getpreferredencoding(False) or "gbk"
    kwargs: dict[str, Any] = {
        "args": argv,
        "check": check,
        "capture_output": True,
        "text": True,
        "encoding": encoding,
        "errors": "replace",
    }
    if sys.platform == "win32":
        # CREATE_NO_WINDOW — schtasks/system helpers otherwise pop black consoles
        # when invoked from the pywebview GUI.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = startupinfo
    return subprocess.run(**kwargs)


def install_service(
    spec: ServiceSpec,
    *,
    start: bool = True,
    boot_policy: str | None = None,
) -> dict[str, Any]:
    policy = normalize_boot_policy(boot_policy)
    if sys.platform == "win32" and policy == "startup" and not is_process_elevated():
        raise HostServiceError("serve_err_startup_needs_admin")
    cmd = launch_command(cli_args=spec.cli_args, boot_policy=policy)
    if sys.platform == "win32":
        receipt = _install_windows(spec, cmd, start=start, boot_policy=policy)
    elif sys.platform.startswith("linux"):
        receipt = _install_linux(spec, cmd, start=start, boot_policy=policy)
    elif sys.platform == "darwin":
        receipt = _install_macos(spec, cmd, start=start, boot_policy=policy)
    else:
        raise RuntimeError(
            f"service install is not supported on this platform ({sys.platform}); "
            "use `argusai serve run` in a terminal, or register the OS service manually."
        )
    save_host_service_prefs(
        {
            "enabled": True,
            "unit": spec.unit,
            "cron_only": spec.unit == HOST_CRON_ONLY_SERVICE.unit,
            "boot_policy": policy,
            "packaged": is_packaged_runtime(),
            "command": cmd,
        }
    )
    receipt["boot_policy"] = policy
    receipt["packaged"] = is_packaged_runtime()
    warnings = launch_environment_warnings()
    if warnings:
        receipt["warnings"] = warnings
    return receipt


def uninstall_service(spec: ServiceSpec) -> dict[str, Any]:
    if sys.platform == "win32":
        return _uninstall_windows(spec)
    if sys.platform.startswith("linux"):
        return _uninstall_linux(spec)
    if sys.platform == "darwin":
        return _uninstall_macos(spec)
    raise RuntimeError(
        f"service uninstall is not supported on this platform ({sys.platform})"
    )


def service_plan(spec: ServiceSpec, *, boot_policy: str | None = None) -> dict[str, Any]:
    policy = normalize_boot_policy(boot_policy or load_host_service_prefs().get("boot_policy"))
    cmd = launch_command(cli_args=spec.cli_args, boot_policy=policy)
    warnings = launch_environment_warnings()
    if sys.platform == "win32":
        cron_only = spec.unit == HOST_CRON_ONLY_SERVICE.unit
        if policy == "logon" and not cron_only:
            return {
                "ok": True,
                "platform": "windows",
                "kind": "run_key",
                "name": "HKCU\\...\\Run\\ArgusAI",
                "command": cmd,
                "command_line": launch_command_line(
                    cli_args=spec.cli_args, boot_policy=policy
                ),
                "boot_policy": policy,
                "trigger": "hkcu_run",
                "packaged": is_packaged_runtime(),
                "warnings": warnings,
                "note": "Per-user Run key; appears in Settings → Apps → Startup.",
            }
        return {
            "ok": True,
            "platform": "windows",
            "kind": "scheduled_task",
            "name": _win_task_name(spec),
            "command": cmd,
            "command_line": launch_command_line(
                cli_args=spec.cli_args, boot_policy=policy
            ),
            "boot_policy": policy,
            "trigger": "at_startup" if policy == "startup" else "at_logon",
            "packaged": is_packaged_runtime(),
            "warnings": warnings,
        }
    if sys.platform.startswith("linux"):
        path = _linux_unit_path(spec)
        return {
            "ok": True,
            "platform": "linux",
            "kind": "systemd_user",
            "unit": spec.unit,
            "path": str(path),
            "command": cmd,
            "unit_body": _linux_unit_body(spec, cmd),
            "boot_policy": policy,
            "packaged": is_packaged_runtime(),
            "warnings": warnings,
            "note": (
                "will run loginctl enable-linger"
                if policy == "linger"
                else "starts with the user session (no linger)"
            ),
        }
    if sys.platform == "darwin":
        from argusai.cli.macos_login_item import available_for_host, login_item_status

        cron_only = spec.unit == HOST_CRON_ONLY_SERVICE.unit
        if available_for_host(cron_only=cron_only):
            login = login_item_status()
            return {
                "ok": True,
                "platform": "darwin",
                "kind": "login_item",
                "api": "SMAppService.mainApp",
                "label": "ArgusAI",
                "unit": spec.unit,
                "bundle": str(login.get("bundle") or ""),
                "command": cmd,
                "command_line": launch_command_line(
                    cli_args=spec.cli_args, boot_policy=policy
                ),
                "boot_policy": policy,
                "packaged": is_packaged_runtime(),
                "warnings": warnings,
                "login_item": login,
                "note": (
                    "Login Item (System Settings → General → Login Items). "
                    "No admin. Starts the app hidden after you log in."
                ),
            }
        path = _macos_plist_path(spec)
        return {
            "ok": True,
            "platform": "darwin",
            "kind": "launch_agent",
            "label": _macos_label(spec),
            "unit": spec.unit,
            "path": str(path),
            "command": cmd,
            "command_line": launch_command_line(
                cli_args=spec.cli_args, boot_policy=policy
            ),
            "plist_body": _macos_plist_body(spec, cmd),
            "boot_policy": policy,
            "packaged": is_packaged_runtime(),
            "warnings": warnings,
            "note": "LaunchAgent starts after you log in (gui session, no admin)",
        }
    return {
        "ok": False,
        "platform": sys.platform,
        "error": "unsupported platform",
        "command": cmd,
        "warnings": warnings,
    }


def service_is_installed(spec: ServiceSpec) -> bool:
    if sys.platform == "win32":
        if spec.unit == HOST_SERVICE.unit:
            from argusai.cli.windows_run_key import run_key_is_registered

            if run_key_is_registered():
                return True
        return _win_task_probe(_win_task_name(spec))[0]
    if sys.platform.startswith("linux"):
        return _linux_unit_path(spec).exists()
    if sys.platform == "darwin":
        if spec.unit == HOST_SERVICE.unit:
            from argusai.cli.macos_login_item import login_item_status

            if login_item_status().get("registered"):
                return True
        return _macos_plist_path(spec).exists()
    return False


def resolve_installed_host() -> ServiceSpec | None:
    return _resolve_installed_host_probe()[0]


def _resolve_installed_host_probe() -> tuple[ServiceSpec | None, str]:
    """Installed spec plus its Windows status value, from one probe per spec."""
    for spec in (HOST_SERVICE, HOST_CRON_ONLY_SERVICE):
        if sys.platform == "win32":
            if spec.unit == HOST_SERVICE.unit:
                from argusai.cli.windows_run_key import run_key_is_registered

                if run_key_is_registered():
                    return spec, "run_key"
            exists, state = _win_task_probe(_win_task_name(spec))
            if exists:
                return spec, state
            continue
        if service_is_installed(spec):
            return spec, ""
    return None, ""


def service_status(spec: ServiceSpec | None = None) -> dict[str, Any]:
    prefs = load_host_service_prefs()
    if spec is None:
        target, probed_state = _resolve_installed_host_probe()
    else:
        target, probed_state = spec, ""
    policy = normalize_boot_policy(prefs.get("boot_policy"))
    priv = privilege_probe(boot_policy=policy)
    if target is None:
        return {
            "ok": True,
            "installed": False,
            "active": False,
            "state": "not_installed",
            "unit": None,
            "boot_policy": policy,
            "platform": sys.platform,
            **priv,
        }
    if sys.platform == "win32":
        out = _status_windows(target, state=probed_state)
    elif sys.platform.startswith("linux"):
        out = _status_linux(target)
    elif sys.platform == "darwin":
        out = _status_macos(target)
    else:
        return {
            "ok": False,
            "installed": False,
            "error": f"unsupported platform ({sys.platform})",
            "boot_policy": policy,
            **priv,
        }
    out["boot_policy"] = policy
    out["platform"] = sys.platform
    out.update(priv)
    return out


def start_service(spec: ServiceSpec | None = None) -> dict[str, Any]:
    target = spec or resolve_installed_host()
    if target is None:
        raise RuntimeError("no ArgusAI host service installed; run: argusai serve install")
    if sys.platform == "win32":
        from argusai.cli.windows_run_key import run_key_is_registered, start_run_key_command

        if target.unit == HOST_SERVICE.unit and run_key_is_registered():
            cmd = launch_command(
                cli_args=target.cli_args,
                boot_policy=normalize_boot_policy("logon"),
            )
            opened = start_run_key_command(cmd)
            if not opened.get("ok"):
                raise HostServiceError(
                    "serve_err_unknown",
                    opened.get("error") or "failed to start ArgusAI",
                )
            return {"ok": True, "action": "start", "kind": "run_key", "name": "ArgusAI"}
        proc = _run(["schtasks", "/Run", "/TN", _win_task_name(target)], check=False)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            if _win_access_denied(err):
                raise HostServiceError("serve_err_access_denied_logon")
            raise HostServiceError("serve_err_unknown", f"schtasks run failed: {err}")
        return {"ok": True, "action": "start", "name": _win_task_name(target)}
    if sys.platform.startswith("linux"):
        proc = _run(["systemctl", "--user", "start", f"{target.unit}.service"], check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"systemctl start failed: {(proc.stderr or proc.stdout or '').strip()}"
            )
        return {"ok": True, "action": "start", "unit": target.unit}
    if sys.platform == "darwin":
        return _start_macos(target)
    raise RuntimeError(f"unsupported platform ({sys.platform})")


def stop_service(spec: ServiceSpec | None = None) -> dict[str, Any]:
    if spec is None:
        target, state = _resolve_installed_host_probe()
    else:
        target, state = spec, ""
    if target is None:
        raise RuntimeError("no ArgusAI host service installed")
    if sys.platform == "win32":
        from argusai.cli.windows_run_key import run_key_is_registered

        if target.unit == HOST_SERVICE.unit and (
            state == "run_key" or run_key_is_registered()
        ):
            return {
                "ok": True,
                "action": "stop",
                "kind": "run_key",
                "name": "ArgusAI",
                "was_running": False,
            }
        name = _win_task_name(target)
        running = _win_status_is_active(state) if state else None
        # /End fails when not running — treat as stopped.
        stopped = _end_windows_task(name, running=running)
        return {
            "ok": True,
            "action": "stop",
            "name": name,
            "was_running": stopped,
        }
    if sys.platform.startswith("linux"):
        proc = _run(["systemctl", "--user", "stop", f"{target.unit}.service"], check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"systemctl stop failed: {(proc.stderr or proc.stdout or '').strip()}"
            )
        return {"ok": True, "action": "stop", "unit": target.unit}
    if sys.platform == "darwin":
        return _stop_macos(target)
    raise RuntimeError(f"unsupported platform ({sys.platform})")


def restart_service(spec: ServiceSpec | None = None) -> dict[str, Any]:
    target = spec or resolve_installed_host()
    if target is None:
        raise RuntimeError("no ArgusAI host service installed; run: argusai serve install")
    if sys.platform.startswith("linux"):
        proc = _run(
            ["systemctl", "--user", "restart", f"{target.unit}.service"], check=False
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"systemctl restart failed: {(proc.stderr or proc.stdout or '').strip()}"
            )
        return {"ok": True, "action": "restart", "unit": target.unit}
    if sys.platform == "darwin":
        from argusai.cli.macos_login_item import login_item_status

        if target.unit == HOST_SERVICE.unit and login_item_status().get("registered"):
            return {**_start_macos(target), "action": "restart"}
        loaded, _ = _macos_is_loaded(target)
        if loaded:
            proc = _macos_kickstart(target)
            if proc.returncode != 0:
                raise RuntimeError(
                    "launchctl kickstart failed: "
                    f"{(proc.stderr or proc.stdout or '').strip() or f'exit {proc.returncode}'}"
                )
            return {
                "ok": True,
                "action": "restart",
                "name": _macos_label(target),
                "unit": target.unit,
            }
        return {**_start_macos(target), "action": "restart"}
    # Windows: end (and wait for the process to settle) then run.
    stop_service(target)
    return {**start_service(target), "action": "restart"}


def uninstall_legacy_services() -> list[str]:
    removed: list[str] = []
    for spec in LEGACY_SERVICES:
        try:
            receipt = uninstall_service(spec)
        except Exception:
            continue
        if receipt.get("removed"):
            removed.append(str(receipt.get("name") or receipt.get("unit") or spec.unit))
    return removed


def uninstall_alternate_host(spec: ServiceSpec) -> list[str]:
    other = HOST_CRON_ONLY_SERVICE if spec.unit == HOST_SERVICE.unit else HOST_SERVICE
    if other.unit == spec.unit:
        return []
    try:
        receipt = uninstall_service(other)
    except Exception:
        return []
    if receipt.get("removed"):
        return [str(receipt.get("name") or receipt.get("unit") or other.unit)]
    return []


def _install_windows(
    spec: ServiceSpec,
    cmd: list[str],
    *,
    start: bool,
    boot_policy: str,
) -> dict[str, Any]:
    from argusai.cli.windows_run_key import (
        delete_run_key,
        run_key_command_line,
        start_run_key_command,
        write_run_key,
    )

    cron_only = spec.unit == HOST_CRON_ONLY_SERVICE.unit
    if boot_policy == "logon" and not cron_only:
        _delete_windows_task(spec)
        written = write_run_key(run_key_command_line(cmd))
        if not written.get("ok"):
            raise HostServiceError(
                "serve_err_unknown",
                written.get("error") or "failed to write the Windows Run key",
            )
        started = False
        if start:
            started = bool(start_run_key_command(cmd).get("ok"))
        return {
            "ok": True,
            "platform": "windows",
            "kind": "run_key",
            "name": "ArgusAI",
            "command": cmd,
            "started": started,
            "boot_policy": boot_policy,
            "hidden": True,
        }

    delete_run_key()
    name = _win_task_name(spec)
    exists, state = _win_task_probe(name)
    if exists:
        # ``/Delete`` leaves an already-running action alive, which would fight
        # the replacement over the channel host lock. End it first.
        _end_windows_task(name, running=_win_status_is_active(state))
        _run(["schtasks", "/Delete", "/F", "/TN", name], check=False)

    # BootTrigger needs an S4U principal to run before logon; S4U registration
    # can be refused (policy / no batch-logon right), so fall back rather than
    # leaving the user without any autostart.
    logon_types = ["S4U", "InteractiveToken"] if boot_policy == "startup" else ["InteractiveToken"]
    proc: subprocess.CompletedProcess[str] | None = None
    used_logon = logon_types[0]
    notes: list[str] = []
    for index, logon_type in enumerate(logon_types):
        proc = _create_windows_task(
            spec, cmd, boot_policy=boot_policy, logon_type=logon_type, name=name
        )
        used_logon = logon_type
        if proc.returncode == 0:
            if index:
                from argusai.cli.locale import label

                notes.append(label("serve_note_s4u_fallback"))
            break
    assert proc is not None
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        if _win_access_denied(err) or boot_policy == "startup":
            raise HostServiceError(
                "serve_err_startup_needs_admin"
                if boot_policy == "startup"
                else "serve_err_access_denied_logon"
            )
        raise HostServiceError("serve_err_unknown", f"schtasks create failed: {err}")
    started = False
    if start:
        run = _run(["schtasks", "/Run", "/TN", name], check=False)
        started = run.returncode == 0
    receipt: dict[str, Any] = {
        "ok": True,
        "platform": "windows",
        "kind": "scheduled_task",
        "name": name,
        "command": cmd,
        "started": started,
        "boot_policy": boot_policy,
        "logon_type": used_logon,
        "hidden": True,
        "detail": (proc.stdout or "").strip(),
    }
    if notes:
        receipt["note"] = " ".join(notes)
    return receipt


def _create_windows_task(
    spec: ServiceSpec,
    cmd: list[str],
    *,
    boot_policy: str,
    logon_type: str,
    name: str,
) -> subprocess.CompletedProcess[str]:
    import tempfile

    xml_body = _windows_task_xml(
        spec, cmd, boot_policy=boot_policy, logon_type=logon_type
    )
    # schtasks /XML expects UTF-16 LE on Windows.
    xml_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".xml",
            delete=False,
            prefix="argusai-task-",
        ) as fh:
            fh.write(xml_body.encode("utf-16"))
            xml_path = Path(fh.name)
        create = ["schtasks", "/Create", "/F", "/TN", name, "/XML", str(xml_path)]
        return _run(create, check=False)
    finally:
        if xml_path is not None:
            try:
                xml_path.unlink(missing_ok=True)
            except OSError:
                pass


def _end_windows_task(name: str, *, running: bool | None = None) -> bool:
    """End a running action and wait for it to settle.

    ``running`` lets callers pass a probe they already paid for; when the task
    is known to be idle this costs zero extra ``schtasks`` processes. Waiting is
    a handful of backoff checks rather than a tight poll — every check spawns a
    console process, and a storm of them is what users see as flashing windows.
    """
    if running is None:
        running = _win_task_is_running(name)
    if not running:
        return False
    proc = _run(["schtasks", "/End", "/TN", name], check=False)
    if proc.returncode != 0:
        return False
    for delay in (0.4, 0.8, 1.2):
        time.sleep(delay)
        if not _win_task_is_running(name):
            return True
    return False


def _delete_windows_task(spec: ServiceSpec) -> bool:
    name = _win_task_name(spec)
    exists, state = _win_task_probe(name)
    if not exists:
        return False
    _end_windows_task(name, running=_win_status_is_active(state))
    proc = _run(["schtasks", "/Delete", "/F", "/TN", name], check=False)
    return proc.returncode == 0


def _uninstall_windows(spec: ServiceSpec) -> dict[str, Any]:
    from argusai.cli.windows_run_key import delete_run_key

    run_removed = False
    if spec.unit == HOST_SERVICE.unit:
        run_removed = bool(delete_run_key().get("removed"))
    name = _win_task_name(spec)
    exists, state = _win_task_probe(name)
    if not exists:
        return {
            "ok": True,
            "platform": "windows",
            "name": name,
            "removed": run_removed,
            "run_key_removed": run_removed,
        }
    # Stop the host before dropping the registration, otherwise "removed"
    # leaves an orphan process still holding channels.
    _end_windows_task(name, running=_win_status_is_active(state))
    proc = _run(["schtasks", "/Delete", "/F", "/TN", name], check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        lowered = err.lower()
        if "cannot find" in lowered or "找不到" in err or "does not exist" in lowered:
            return {
                "ok": True,
                "platform": "windows",
                "name": name,
                "removed": run_removed,
                "run_key_removed": run_removed,
            }
        if _win_access_denied(err):
            raise HostServiceError("serve_err_access_denied_delete")
        raise HostServiceError("serve_err_unknown", f"schtasks delete failed: {err}")
    return {
        "ok": True,
        "platform": "windows",
        "name": name,
        "removed": True,
        "run_key_removed": run_removed,
    }


def _win_access_denied(text: str) -> bool:
    lowered = (text or "").lower()
    return (
        "access is denied" in lowered
        or "拒绝访问" in text
        or "拒絕存取" in text
        or "0x80070005" in lowered
    )


def _win_error_hint(text: str) -> str:
    """Turn a raw schtasks failure into something the user can act on."""
    from argusai.cli.locale import label

    message = (text or "").strip() or label("serve_err_unknown")
    if _win_access_denied(message):
        return label("serve_err_access_denied_logon")
    return message


def _install_linux(
    spec: ServiceSpec,
    cmd: list[str],
    *,
    start: bool,
    boot_policy: str,
) -> dict[str, Any]:
    path = _linux_unit_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_linux_unit_body(spec, cmd), encoding="utf-8")
    reload = _run(["systemctl", "--user", "daemon-reload"], check=False)
    if reload.returncode != 0:
        raise RuntimeError(
            "wrote unit file but systemctl --user daemon-reload failed: "
            f"{(reload.stderr or reload.stdout or '').strip()}"
        )
    enable_argv = ["systemctl", "--user", "enable"]
    if start:
        enable_argv.append("--now")
    enable_argv.append(f"{spec.unit}.service")
    enable = _run(enable_argv, check=False)
    if enable.returncode != 0:
        raise RuntimeError(
            f"systemctl enable failed: {(enable.stderr or enable.stdout or '').strip()}"
        )
    linger_note = "starts with the user session"
    if boot_policy == "linger":
        linger = _run(["loginctl", "enable-linger", str(os.getuid())], check=False)
        if linger.returncode != 0:
            # Fallback to username — some distros prefer it.
            linger = _run(
                ["loginctl", "enable-linger", os.environ.get("USER") or ""],
                check=False,
            )
        if linger.returncode != 0:
            raise RuntimeError(
                "unit installed but loginctl enable-linger failed: "
                f"{(linger.stderr or linger.stdout or '').strip()}"
            )
        linger_note = "lingering enabled (survives logout / boots with system user manager)"
    return {
        "ok": True,
        "platform": "linux",
        "kind": "systemd_user",
        "unit": spec.unit,
        "path": str(path),
        "command": cmd,
        "started": bool(start),
        "boot_policy": boot_policy,
        "note": linger_note,
    }


def _uninstall_linux(spec: ServiceSpec) -> dict[str, Any]:
    path = _linux_unit_path(spec)
    _run(
        ["systemctl", "--user", "disable", "--now", f"{spec.unit}.service"],
        check=False,
    )
    _run(["systemctl", "--user", "daemon-reload"], check=False)
    removed = False
    if path.exists():
        path.unlink()
        removed = True
    return {
        "ok": True,
        "platform": "linux",
        "unit": spec.unit,
        "path": str(path),
        "removed": removed,
    }


def _macos_cmd_error(proc: subprocess.CompletedProcess[str], action: str) -> str:
    err = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
    lowered = err.lower()
    if "could not find domain" in lowered or "bootstrap failed: 5" in lowered:
        return (
            f"{action} failed: {err}. LaunchAgents need a logged-in macOS GUI "
            "session (the user desktop), not a headless SSH login."
        )
    return f"{action} failed: {err}"


def _macos_drop_launch_agent(spec: ServiceSpec) -> bool:
    path = _macos_plist_path(spec)
    _macos_bootout(spec)
    if path.exists():
        path.unlink()
        return True
    return False


def _install_macos_login_item(
    spec: ServiceSpec,
    cmd: list[str],
    *,
    start: bool,
    boot_policy: str,
) -> dict[str, Any] | None:
    from argusai.cli.macos_login_item import (
        available_for_host,
        open_app_hidden,
        register_main_app,
    )

    cron_only = spec.unit == HOST_CRON_ONLY_SERVICE.unit
    if not available_for_host(cron_only=cron_only):
        return None
    # A leftover LaunchAgent plus a Login Item would start two hosts.
    _macos_drop_launch_agent(spec)
    result = register_main_app()
    if not result.get("ok"):
        return None
    started = False
    if start:
        opened = open_app_hidden()
        started = bool(opened.get("ok"))
    note = "Login Item starts ArgusAI after you log in (no admin)."
    if result.get("requires_approval"):
        note = (
            "Registered. Allow ArgusAI in System Settings → General → "
            "Login Items & Extensions."
        )
    return {
        "ok": True,
        "platform": "darwin",
        "kind": "login_item",
        "api": "SMAppService.mainApp",
        "label": "ArgusAI",
        "name": "ArgusAI",
        "unit": spec.unit,
        "command": cmd,
        "started": started,
        "boot_policy": boot_policy,
        "login_item_status": result.get("status"),
        "requires_approval": bool(result.get("requires_approval")),
        "bundle": result.get("bundle") or "",
        "note": note,
    }


def _install_macos(
    spec: ServiceSpec,
    cmd: list[str],
    *,
    start: bool,
    boot_policy: str,
) -> dict[str, Any]:
    from argusai.cli.macos_login_item import unregister_main_app

    login_receipt = _install_macos_login_item(
        spec, cmd, start=start, boot_policy=boot_policy
    )
    if login_receipt is not None:
        return login_receipt

    # Dev / older macOS / cron-only: user LaunchAgent. Drop a leftover
    # Login Item so login does not start both the app and this unit.
    if spec.unit == HOST_SERVICE.unit:
        unregister_main_app()
    path = _macos_plist_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    _macos_log_dir().mkdir(parents=True, exist_ok=True)
    path.write_text(_macos_plist_body(spec, cmd), encoding="utf-8")
    # Drop a previous bootstrap so the new plist is what launchd sees.
    _macos_bootout(spec)
    started = False
    if start:
        boot = _macos_bootstrap(spec, path)
        if boot.returncode != 0 and not _macos_already_loaded(
            boot.stderr or boot.stdout or ""
        ):
            raise RuntimeError(_macos_cmd_error(boot, "launchctl bootstrap"))
        _run(["launchctl", "enable", _macos_service_target(spec)], check=False)
        kick = _macos_kickstart(spec)
        started = kick.returncode == 0 or _macos_text_is_active(
            _macos_is_loaded(spec)[1]
        )
        if not started and kick.returncode != 0:
            raise RuntimeError(_macos_cmd_error(kick, "launchctl kickstart"))
    return {
        "ok": True,
        "platform": "darwin",
        "kind": "launch_agent",
        "label": _macos_label(spec),
        "name": _macos_label(spec),
        "unit": spec.unit,
        "path": str(path),
        "command": cmd,
        "started": started,
        "boot_policy": boot_policy,
        "note": "LaunchAgent starts after you log in (no admin)",
    }


def _uninstall_macos(spec: ServiceSpec) -> dict[str, Any]:
    from argusai.cli.macos_login_item import unregister_main_app

    login_removed = False
    if spec.unit == HOST_SERVICE.unit:
        login_removed = bool(unregister_main_app().get("removed"))
    agent_removed = _macos_drop_launch_agent(spec)
    return {
        "ok": True,
        "platform": "darwin",
        "label": _macos_label(spec),
        "name": _macos_label(spec),
        "unit": spec.unit,
        "path": str(_macos_plist_path(spec)),
        "removed": login_removed or agent_removed,
        "login_item_removed": login_removed,
        "launch_agent_removed": agent_removed,
    }


def _start_macos(spec: ServiceSpec) -> dict[str, Any]:
    from argusai.cli.macos_login_item import login_item_status, open_app_hidden

    login = login_item_status()
    if spec.unit == HOST_SERVICE.unit and login.get("registered"):
        opened = open_app_hidden()
        if not opened.get("ok"):
            raise RuntimeError(opened.get("error") or "failed to open ArgusAI.app")
        return {
            "ok": True,
            "action": "start",
            "kind": "login_item",
            "name": "ArgusAI",
            "unit": spec.unit,
            "bundle": login.get("bundle") or "",
        }
    path = _macos_plist_path(spec)
    if not path.exists():
        raise RuntimeError("no ArgusAI host service installed; run: argusai serve install")
    loaded, detail = _macos_is_loaded(spec)
    if not loaded:
        boot = _macos_bootstrap(spec, path)
        if boot.returncode != 0 and not _macos_already_loaded(
            boot.stderr or boot.stdout or ""
        ):
            raise RuntimeError(_macos_cmd_error(boot, "launchctl bootstrap"))
        _run(["launchctl", "enable", _macos_service_target(spec)], check=False)
    kick = _macos_kickstart(spec)
    if kick.returncode != 0:
        loaded, detail = _macos_is_loaded(spec)
        if not _macos_text_is_active(detail):
            raise RuntimeError(_macos_cmd_error(kick, "launchctl kickstart"))
    return {
        "ok": True,
        "action": "start",
        "kind": "launch_agent",
        "name": _macos_label(spec),
        "unit": spec.unit,
        "path": str(path),
    }


def _stop_macos(spec: ServiceSpec) -> dict[str, Any]:
    from argusai.cli.macos_login_item import login_item_status

    loaded, _ = _macos_is_loaded(spec)
    _macos_bootout(spec)
    login = login_item_status() if spec.unit == HOST_SERVICE.unit else {}
    return {
        "ok": True,
        "action": "stop",
        "name": _macos_label(spec),
        "unit": spec.unit,
        "was_running": loaded,
        "login_item_registered": bool(login.get("registered")),
    }


def _status_macos(spec: ServiceSpec) -> dict[str, Any]:
    from argusai.cli.macos_login_item import login_item_status

    path = _macos_plist_path(spec)
    agent_installed = path.exists()
    loaded, detail = _macos_is_loaded(spec)
    agent_active = _macos_text_is_active(detail) if loaded else False
    login = (
        login_item_status()
        if spec.unit == HOST_SERVICE.unit
        else {"registered": False, "enabled": False, "requires_approval": False, "status": ""}
    )
    login_registered = bool(login.get("registered"))
    installed = login_registered or agent_installed
    if login_registered and not agent_installed:
        kind = "login_item"
        active = bool(login.get("enabled"))
        if login.get("requires_approval"):
            state = "needs_approval"
        elif login.get("enabled"):
            state = "enabled"
        else:
            state = "registered"
    elif agent_installed:
        kind = "launch_agent"
        active = agent_active
        if agent_active:
            state = "running"
        elif loaded:
            state = "loaded"
        else:
            state = "stopped"
    else:
        kind = "none"
        active = False
        state = "not_installed"
    return {
        "ok": True,
        "installed": installed,
        "active": active,
        "state": state,
        "kind": kind,
        "label": "ArgusAI" if kind == "login_item" else _macos_label(spec),
        "name": "ArgusAI" if kind == "login_item" else _macos_label(spec),
        "unit": spec.unit,
        "path": str(path),
        "bundle": str(login.get("bundle") or ""),
        "login_item_status": login.get("status") or "",
        "requires_approval": bool(login.get("requires_approval")),
        "cron_only": spec.unit == HOST_CRON_ONLY_SERVICE.unit,
        "detail": (detail or "")[:2000],
    }


# schtasks localizes the status value. Field *names* are localized too
# ("Status" / "模式" / "État"), so the value is read positionally from CSV and
# only the wording is matched here.
_WIN_RUNNING_STATES = frozenset(
    {
        "running",
        "正在运行",
        "執行中",
        "実行中",
        "실행 중",
        "wird ausgeführt",
        "en cours d'exécution",
        "en ejecución",
        "in esecuzione",
        "выполняется",
    }
)
_WIN_IDLE_STATES = frozenset(
    {
        "ready",
        "就绪",
        "准备就绪",
        "就緒",
        "disabled",
        "已禁用",
        "queued",
        "已排队",
        "bereit",
        "prêt",
        "listo",
    }
)


def _win_task_probe(name: str) -> tuple[bool, str]:
    """``(exists, status_value)`` from a single ``schtasks /Query``.

    Every probe spawns a console process, so callers share one query instead of
    asking "is it installed?" and "is it running?" separately.
    """
    import csv
    import io

    proc = _run(["schtasks", "/Query", "/TN", name, "/FO", "CSV", "/NH"], check=False)
    if proc.returncode != 0:
        return False, ""
    for row in csv.reader(io.StringIO(proc.stdout or "")):
        if len(row) >= 3 and row[0].strip():
            return True, row[2].strip()
    return True, ""


def _win_task_status_value(name: str) -> str:
    """Status column of ``schtasks /Query`` (CSV column order is locale-stable)."""
    return _win_task_probe(name)[1]


def _win_status_is_active(value: str) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return False
    if text in _WIN_RUNNING_STATES:
        return True
    if text in _WIN_IDLE_STATES:
        return False
    # Unknown localization: never let "Not Running" read as running.
    return "running" in text and "not running" not in text


def _win_task_is_running(name: str) -> bool:
    return _win_status_is_active(_win_task_status_value(name))


def _status_windows(spec: ServiceSpec, *, state: str = "") -> dict[str, Any]:
    if spec.unit == HOST_SERVICE.unit:
        from argusai.cli.windows_run_key import run_key_status

        run = run_key_status()
        if run.get("registered") or state == "run_key":
            enabled = bool(run.get("enabled"))
            return {
                "ok": True,
                "installed": True,
                "active": enabled,
                "state": "enabled" if enabled else "disabled_in_settings",
                "kind": "run_key",
                "name": "ArgusAI",
                "unit": spec.unit,
                "command": run.get("command") or "",
                "cron_only": False,
            }
    name = _win_task_name(spec)
    proc = _run(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"], check=False)
    if proc.returncode != 0:
        return {
            "ok": True,
            "installed": False,
            "active": False,
            "state": "not_installed",
            "kind": "scheduled_task",
            "name": name,
            "unit": spec.unit,
        }
    text = proc.stdout or ""
    status_line = state or _win_task_status_value(name)
    return {
        "ok": True,
        "installed": True,
        "active": _win_status_is_active(status_line),
        "state": status_line or "unknown",
        "kind": "scheduled_task",
        "name": name,
        "unit": spec.unit,
        "cron_only": spec.unit == HOST_CRON_ONLY_SERVICE.unit,
        "detail": text.strip()[:2000],
    }


def _status_linux(spec: ServiceSpec) -> dict[str, Any]:
    path = _linux_unit_path(spec)
    installed = path.exists()
    active_proc = _run(
        ["systemctl", "--user", "is-active", f"{spec.unit}.service"], check=False
    )
    state = (active_proc.stdout or "").strip() or "unknown"
    return {
        "ok": True,
        "installed": installed,
        "active": state == "active",
        "state": state if installed else "not_installed",
        "unit": spec.unit,
        "path": str(path),
        "cron_only": spec.unit == HOST_CRON_ONLY_SERVICE.unit,
    }
