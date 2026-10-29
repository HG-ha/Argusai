"""HostCallbacks / HostKind unit tests."""

from __future__ import annotations

from unittest import mock

from argusai.core.host import (
    host_supports_webview,
    log_session_updated,
    normalize_host_kind,
    noop_attention,
    resolve_launch_mode,
)


def test_normalize_host_kind() -> None:
    assert normalize_host_kind("GUI") == "gui"
    assert normalize_host_kind("headless") == "headless"
    assert normalize_host_kind("cli") == "cli"
    assert normalize_host_kind("weird") == "gui"
    assert normalize_host_kind(None, default="headless") == "headless"


def test_host_supports_webview() -> None:
    assert host_supports_webview("gui") is True
    assert host_supports_webview("headless") is False
    assert host_supports_webview("cli") is False


def test_resolve_launch_mode_force_and_env() -> None:
    assert resolve_launch_mode(force="gui") == "gui"
    assert resolve_launch_mode(force="chat") == "cli"
    assert resolve_launch_mode(force=None, environ={"ARGUSAI_HOST": "gui"}) == "gui"
    assert resolve_launch_mode(force=None, environ={"ARGUSAI_HOST": "cli"}) == "cli"


def test_resolve_launch_mode_tty_defaults_to_cli() -> None:
    assert (
        resolve_launch_mode(
            force=None,
            environ={},
            interactive=True,
            gui_available=True,
            packaged=False,
        )
        == "cli"
    )
    assert (
        resolve_launch_mode(
            force=None,
            environ={},
            interactive=False,
            gui_available=True,
            packaged=False,
        )
        == "gui"
    )
    assert (
        resolve_launch_mode(
            force=None,
            environ={},
            interactive=False,
            gui_available=False,
            packaged=False,
        )
        == "cli"
    )


def test_resolve_launch_mode_packaged_console_stays_cli() -> None:
    # Packaged exe from an interactive shell must not jump to the GUI.
    assert (
        resolve_launch_mode(
            force=None,
            environ={},
            interactive=True,
            gui_available=True,
            packaged=True,
        )
        == "cli"
    )
    assert (
        resolve_launch_mode(
            force=None,
            environ={},
            interactive=False,
            gui_available=True,
            packaged=True,
        )
        == "gui"
    )
    assert (
        resolve_launch_mode(
            force=None,
            environ={},
            interactive=True,
            gui_available=False,
            packaged=True,
        )
        == "cli"
    )
    assert (
        resolve_launch_mode(
            force="chat",
            environ={},
            interactive=True,
            gui_available=True,
            packaged=True,
        )
        == "cli"
    )
    assert (
        resolve_launch_mode(
            force="gui",
            environ={},
            interactive=True,
            gui_available=True,
            packaged=True,
        )
        == "gui"
    )


def test_is_packaged_binary_ignores_console_script_name() -> None:
    """pip/uv ``Scripts\\argusai.exe`` is not a Nuitka build."""
    from argusai.core import host as host_mod

    with mock.patch.object(host_mod.sys, "frozen", False, create=True), mock.patch(
        "argusai.core.infra.runtime_exec.is_frozen_runtime", return_value=False
    ), mock.patch.object(host_mod.sys, "argv", ["H:/venv/Scripts/argusai.exe"]), mock.patch.object(
        host_mod.sys, "executable", "H:/venv/Scripts/python.exe"
    ):
        assert host_mod.is_packaged_binary() is False


def _patch_win_console(*, packaged: bool, self_allocated: bool | None):
    """Pretend to be a Windows console session with a known console owner."""
    from argusai.core import host as host_mod

    return (
        mock.patch.object(host_mod.sys, "platform", "win32"),
        mock.patch.object(host_mod, "is_packaged_binary", return_value=packaged),
        mock.patch.object(
            host_mod, "_console_is_self_allocated", return_value=self_allocated
        ),
    )


def test_packaged_exe_on_own_console_is_not_interactive() -> None:
    """Explorer double-click of the console-subsystem build must reach the GUI."""
    from argusai.core import host as host_mod

    tty = mock.Mock(isatty=mock.Mock(return_value=True))
    patches = _patch_win_console(packaged=True, self_allocated=True)
    with patches[0], patches[1], patches[2], mock.patch.object(
        host_mod.sys, "stdin", tty
    ), mock.patch.object(host_mod.sys, "stdout", tty), mock.patch(
        "ctypes.windll.kernel32.GetConsoleWindow", return_value=1, create=True
    ):
        assert host_mod.is_interactive_cli() is False


def test_packaged_exe_on_shared_console_stays_interactive() -> None:
    """Launched from a shell, the same binary must stay on the ops console."""
    from argusai.core import host as host_mod

    tty = mock.Mock(isatty=mock.Mock(return_value=True))
    for verdict in (False, None):
        patches = _patch_win_console(packaged=True, self_allocated=verdict)
        with patches[0], patches[1], patches[2], mock.patch.object(
            host_mod.sys, "stdin", tty
        ), mock.patch.object(host_mod.sys, "stdout", tty), mock.patch(
            "ctypes.windll.kernel32.GetConsoleWindow", return_value=1, create=True
        ):
            assert host_mod.is_interactive_cli() is True, verdict


def test_console_owner_detection_flags_foreign_process() -> None:
    from argusai.core.infra import win_stdio

    with mock.patch.object(win_stdio.sys, "platform", "win32"), mock.patch.object(
        win_stdio, "console_window", return_value=7
    ), mock.patch.object(win_stdio, "console_process_ids", return_value=[10, 20]
    ), mock.patch.object(win_stdio, "_own_image_names", return_value={"argusai.exe"}), mock.patch.object(
        win_stdio.os, "getpid", return_value=10
    ), mock.patch.object(win_stdio, "parent_process_image_name", return_value=""):
        with mock.patch.object(
            win_stdio, "_process_image_name", return_value="ArgusAI.exe"
        ):
            assert win_stdio.console_is_self_allocated() is True
        with mock.patch.object(
            win_stdio, "_process_image_name", return_value="powershell.exe"
        ):
            assert win_stdio.console_is_self_allocated() is False
        # OneFile bootstrap parent often cannot be queried — still ours when
        # every readable peer is our image (and we are on the console list).
        with mock.patch.object(win_stdio, "_process_image_name", return_value=None):
            assert win_stdio.console_is_self_allocated() is True
        with mock.patch.object(win_stdio, "_process_image_name", return_value="cmd.exe"):
            assert win_stdio.console_is_self_allocated() is False
        with mock.patch.object(
            win_stdio, "_process_image_name", return_value="conhost.exe"
        ):
            assert win_stdio.console_is_self_allocated() is True
        with mock.patch.object(
            win_stdio, "_process_image_name", return_value="OpenConsole.exe"
        ):
            assert win_stdio.console_is_self_allocated() is True


def test_explorer_parent_is_a_desktop_console() -> None:
    """Shortcut / double-click parent is Explorer even if WT hosts the console."""
    from argusai.core.infra import win_stdio

    with mock.patch.object(win_stdio.sys, "platform", "win32"), mock.patch.object(
        win_stdio, "console_window", return_value=7
    ), mock.patch.object(
        win_stdio, "parent_process_image_name", return_value="explorer.exe"
    ), mock.patch.object(
        win_stdio, "console_process_ids", return_value=[10, 20, 30]
    ), mock.patch.object(
        win_stdio, "_process_image_name", return_value="WindowsTerminal.exe"
    ):
        assert win_stdio.console_is_self_allocated() is True


def test_shell_parent_keeps_the_console() -> None:
    from argusai.core.infra import win_stdio

    with mock.patch.object(win_stdio.sys, "platform", "win32"), mock.patch.object(
        win_stdio, "console_window", return_value=7
    ), mock.patch.object(
        win_stdio, "parent_process_image_name", return_value="powershell.exe"
    ):
        assert win_stdio.console_is_self_allocated() is False


def test_hide_console_skips_inherited_terminal() -> None:
    """Never hide the window the user is typing in."""
    from argusai.core.infra import win_stdio

    with mock.patch.object(
        win_stdio, "console_has_foreign_shell", return_value=True
    ), mock.patch.object(win_stdio, "console_window", return_value=7) as window:
        assert win_stdio.hide_self_allocated_console() is False
        window.assert_not_called()


def test_windows_terminal_parent_without_a_shell_is_desktop() -> None:
    """Win11 default-terminal hosting is not a user shell."""
    from argusai.core.infra import win_stdio

    with mock.patch.object(win_stdio.sys, "platform", "win32"), mock.patch.object(
        win_stdio, "console_window", return_value=7
    ), mock.patch.object(
        win_stdio, "parent_process_image_name", return_value="WindowsTerminal.exe"
    ), mock.patch.object(win_stdio, "console_process_ids", return_value=[10, 20]
    ), mock.patch.object(win_stdio.os, "getpid", return_value=10), mock.patch.object(
        win_stdio, "_process_image_name", return_value="OpenConsole.exe"
    ):
        assert win_stdio.console_has_foreign_shell() is False
        assert win_stdio.console_is_self_allocated() is True


def test_desktop_console_relaunches_windowless_copy() -> None:
    """Run-key / Explorer consoles are replaced by a CREATE_NO_WINDOW copy."""
    from argusai.core.infra import win_stdio

    env: dict[str, str] = {"NUITKA_ONEFILE_PARENT": "123"}
    with mock.patch.object(win_stdio.sys, "platform", "win32"), mock.patch.dict(
        win_stdio.os.environ, env, clear=True
    ), mock.patch.object(win_stdio, "console_window", return_value=7), mock.patch.object(
        win_stdio, "console_is_self_allocated", return_value=True
    ), mock.patch("subprocess.Popen") as popen:
        assert win_stdio.relaunch_without_console(["--gui", "--tray"]) is True

    popen.assert_called_once()
    args, kwargs = popen.call_args
    assert args[0][-2:] == ["--gui", "--tray"]
    assert kwargs["creationflags"] & 0x08000000
    assert kwargs["env"][win_stdio.CONSOLE_DETACHED_ENV] == "1"
    assert "NUITKA_ONEFILE_PARENT" not in kwargs["env"]


def test_desktop_console_relaunch_skips_shells_and_relaunched_copy() -> None:
    from argusai.core.infra import win_stdio

    with mock.patch.object(win_stdio.sys, "platform", "win32"), mock.patch.object(
        win_stdio, "console_window", return_value=7
    ), mock.patch("subprocess.Popen") as popen:
        with mock.patch.dict(win_stdio.os.environ, {}, clear=True), mock.patch.object(
            win_stdio, "console_is_self_allocated", return_value=False
        ):
            assert win_stdio.relaunch_without_console(["--gui"]) is False
        with mock.patch.dict(
            win_stdio.os.environ, {win_stdio.CONSOLE_DETACHED_ENV: "1"}, clear=True
        ), mock.patch.object(win_stdio, "console_is_self_allocated", return_value=True):
            assert win_stdio.relaunch_without_console(["--gui"]) is False
            # Children of the relaunched GUI must still be able to detach.
            assert win_stdio.CONSOLE_DETACHED_ENV not in win_stdio.os.environ
    popen.assert_not_called()


def test_entry_relaunches_only_bare_desktop_launches() -> None:
    import argusai.__main__ as entry
    from argusai.core.infra import win_stdio

    cases = [
        (["--gui", "--tray"], ["--gui", "--tray"]),
        ([], ["--gui"]),
        (["--tray"], ["--gui", "--tray"]),
        (["scan", "https://example.test"], None),
    ]
    for argv, expected in cases:
        with mock.patch("sys.argv", ["ArgusAI.exe", *argv]), mock.patch(
            "argusai.core.host.resolve_launch_mode", return_value="gui"
        ), mock.patch.object(
            win_stdio, "relaunch_without_console", return_value=True
        ) as relaunch, mock.patch.object(win_stdio, "hide_self_allocated_console") as hide:
            took_over = entry._hide_console_for_desktop_launch()
        if expected is None:
            relaunch.assert_not_called()
            hide.assert_called_once()
            assert took_over is False
        else:
            relaunch.assert_called_once_with(expected)
            hide.assert_not_called()
            assert took_over is True


def test_noop_attention() -> None:
    assert noop_attention(event_key="x", title="t", message="m") is False


def test_headless_session_notify_skips_webview() -> None:
    from argusai.gui.assistant_cron import AssistantCronMixin

    class Fake(AssistantCronMixin):
        host_kind = "headless"
        _window = mock.Mock()

    fake = Fake()
    fake._window.evaluate_js = mock.Mock()
    with mock.patch("argusai.core.host.log_session_updated") as logged:
        fake._notify_assistant_session_updated("sess", reason="test", source_ref="r1")
    fake._window.evaluate_js.assert_not_called()
    logged.assert_called_once()


def test_gui_session_notify_uses_webview() -> None:
    from argusai.gui.assistant_cron import AssistantCronMixin

    class Fake(AssistantCronMixin):
        host_kind = "gui"
        _window = mock.Mock()

    fake = Fake()
    fake._window.evaluate_js = mock.Mock()
    fake._notify_assistant_session_updated("sess", reason="test")
    fake._window.evaluate_js.assert_called_once()


def test_api_for_headless_sets_kind() -> None:
    with mock.patch("argusai.gui.base._ApiBase.__init__", return_value=None):
        from argusai.gui import Api

        # Bypass full construction: only check classmethods wire host_kind kwarg.
        with mock.patch.object(Api, "__init__", return_value=None) as init:
            Api.for_headless()
            init.assert_called_once_with(host_kind="headless")
            init.reset_mock()
            Api.for_cli()
            init.assert_called_once_with(host_kind="cli")
