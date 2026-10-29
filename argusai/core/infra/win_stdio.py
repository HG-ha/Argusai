"""Windows stdio repair and console ownership for packaged processes."""

from __future__ import annotations

import os
import sys
from typing import Any

_NUL_HANDLES: list[int] = []

_SW_HIDE = 0
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def ensure_valid_stdio() -> None:
    """Bind process stdio to NUL when Windows GUI startup leaves invalid handles."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_std_handle = kernel32.GetStdHandle
        get_std_handle.argtypes = [wintypes.DWORD]
        get_std_handle.restype = wintypes.HANDLE
        set_std_handle = kernel32.SetStdHandle
        set_std_handle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
        set_std_handle.restype = wintypes.BOOL
        get_file_type = kernel32.GetFileType
        get_file_type.argtypes = [wintypes.HANDLE]
        get_file_type.restype = wintypes.DWORD
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE

        invalid = wintypes.HANDLE(-1).value
        specs = [
            ((-10) & 0xFFFFFFFF, 0x80000000),  # STD_INPUT_HANDLE, GENERIC_READ
            ((-11) & 0xFFFFFFFF, 0x40000000),  # STD_OUTPUT_HANDLE, GENERIC_WRITE
            ((-12) & 0xFFFFFFFF, 0x40000000),  # STD_ERROR_HANDLE, GENERIC_WRITE
        ]
        for std_id, access in specs:
            current = get_std_handle(std_id)
            if current not in (0, invalid) and get_file_type(current) != 0:
                continue
            handle = create_file("NUL", access, 0x00000003, None, 3, 0, None)
            if handle in (0, invalid):
                continue
            if set_std_handle(std_id, handle):
                _NUL_HANDLES.append(int(handle))
    except Exception:
        return


def quiet_subprocess_kwargs() -> dict[str, Any]:
    """Options for child processes launched from a Windows GUI app."""
    if not sys.platform.startswith("win"):
        return {}
    ensure_valid_stdio()
    import subprocess

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def background_subprocess_kwargs() -> dict[str, Any]:
    """Options for long-lived / GUI children that must outlive the tool call.

    Still uses ``CREATE_NO_WINDOW`` so ``cmd`` / PowerShell *wrappers* do not
    litter empty console windows. Win32 GUI apps (Flet/Tk/Electron) create
    their own HWNDs and are unaffected. Also starts a new process group so
    the child is not tied to the parent's console. Pair with sync
    ``subprocess.Popen`` (not asyncio subprocess) so shutdown does not trip
    closed-loop transport ``__del__``.
    """
    import subprocess

    if sys.platform.startswith("win"):
        ensure_valid_stdio()
        flags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return {"creationflags": flags}
    # POSIX: new session = not killed with the parent's controlling terminal.
    return {"start_new_session": True}


def console_window() -> int:
    """HWND of the console this process is attached to (0 when there is none)."""
    if not sys.platform.startswith("win"):
        return 0
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return 0


def console_process_ids() -> list[int]:
    """PIDs attached to this console, or ``[]`` when that cannot be determined."""
    if not sys.platform.startswith("win"):
        return []
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        list_processes = kernel32.GetConsoleProcessList
        list_processes.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        list_processes.restype = wintypes.DWORD

        size = 8
        for _ in range(4):
            buffer = (wintypes.DWORD * size)()
            count = int(list_processes(buffer, size))
            if count == 0:
                return []
            if count <= size:
                return [int(buffer[i]) for i in range(count)]
            size = count + 4
    except Exception:
        return []
    return []


def _own_image_names() -> set[str]:
    """Executable basenames that belong to this application's own processes."""
    candidates = [sys.executable, sys.argv[0] if sys.argv else ""]
    try:
        from argusai.core.infra.runtime_exec import frozen_self_executable

        candidates.append(frozen_self_executable())
    except Exception:
        pass
    names = {os.path.basename(str(raw or "").strip()).casefold() for raw in candidates}
    names.discard("")
    return names


def _process_image_name(pid: int) -> str | None:
    """Basename of a PID's executable, or ``None`` when it cannot be read."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        query_name = kernel32.QueryFullProcessImageNameW
        query_name.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        query_name.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]

        handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not query_name(handle, 0, buffer, ctypes.byref(size)):
                return None
            return os.path.basename(buffer.value) or None
        finally:
            close_handle(handle)
    except Exception:
        return None


# Interactive shells the user is typing in. Never hide / detach those.
# Windows Terminal / conhost / OpenConsole are hosts, not shells: a desktop
# shortcut on Windows 11 often has them as the parent because WT is the
# default console. Treating them as "foreign" left a blank CMD on screen.
_FOREIGN_SHELLS = frozenset({
    "powershell.exe",
    "pwsh.exe",
    "cmd.exe",
    "bash.exe",
    "wsl.exe",
    "nu.exe",
    "fish.exe",
    "zsh.exe",
})
_FOREIGN_CONSOLE_HOSTS = _FOREIGN_SHELLS

# Console implementation processes. They show up in GetConsoleProcessList on
# Windows 10/11 but are not a user shell — ignore them when deciding ownership.
_CONSOLE_INFRA = frozenset({
    "conhost.exe",
    "openconsole.exe",
    "windowsterminal.exe",
    "wt.exe",
})

_CONSOLE_WINDOW_CLASSES = frozenset({
    "ConsoleWindowClass",
    "CASCADIA_HOSTING_WINDOW_CLASS",
})

# Explorer / Start Menu launched us. Even when Windows Terminal is the default
# console host, the parent of a shortcut/double-click is still one of these.
_DESKTOP_PARENTS = frozenset({
    "explorer.exe",
    "startexperiencehost.exe",
    "searchexperiencehost.exe",
    "sihost.exe",
    "applicationframehost.exe",
})


def parent_process_id() -> int | None:
    """PID of this process's parent, or ``None`` when it cannot be read."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x2

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_snap = kernel32.CreateToolhelp32Snapshot
        create_snap.argtypes = [wintypes.DWORD, wintypes.DWORD]
        create_snap.restype = wintypes.HANDLE
        process32_first = kernel32.Process32FirstW
        process32_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        process32_first.restype = wintypes.BOOL
        process32_next = kernel32.Process32NextW
        process32_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        process32_next.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]

        snap = create_snap(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == wintypes.HANDLE(-1).value:
            return None
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not process32_first(snap, ctypes.byref(entry)):
                return None
            my_pid = os.getpid()
            while True:
                if int(entry.th32ProcessID) == my_pid:
                    return int(entry.th32ParentProcessID)
                if not process32_next(snap, ctypes.byref(entry)):
                    return None
        finally:
            close_handle(snap)
    except Exception:
        return None


def parent_process_image_name() -> str | None:
    """Basename of the parent executable, or ``None`` when unknown."""
    pid = parent_process_id()
    if not pid:
        return None
    return _process_image_name(pid)


def console_is_self_allocated() -> bool | None:
    """True when Windows created this console *for us* (Explorer double-click).

    The packaged Windows build is compiled console-subsystem so that a terminal
    actually waits for it and keeps feeding it keystrokes; a GUI-subsystem
    binary hands the shell back its prompt and the ops console then paints onto
    a console it can never read from. The price is that a double-click also
    gets a console window, which we may hide — but only once we know no foreign
    process shares it.

    Nuitka ``--onefile`` leaves the bootstrap parent attached alongside the
    child (``GetConsoleProcessList`` count ≥ 2). Nuitka's own
    ``hideConsoleIfSpawned`` only hides when count ≤ 1, so this path must
    tolerate a same-named sibling — including when the parent's image name
    cannot be queried.

    ``None`` means undecidable. Callers must treat that like an inherited
    console: never hide a window somebody else may be typing into.
    """
    if not sys.platform.startswith("win"):
        return None
    if not console_window():
        return None
    if console_has_foreign_shell():
        return False
    parent = (parent_process_image_name() or "").casefold()
    if parent in _DESKTOP_PARENTS:
        return True
    pids = console_process_ids()
    own = _own_image_names()
    if not pids or not own:
        return True
    my_pid = os.getpid()
    pid_set = {int(p) for p in pids}
    if my_pid not in pid_set:
        return True
    saw_unreadable = False
    for pid in pid_set:
        if pid == my_pid:
            continue
        name = _process_image_name(pid)
        if name is None:
            saw_unreadable = True
            continue
        folded = name.casefold()
        if folded in _CONSOLE_INFRA or folded in own:
            continue
        if folded in _FOREIGN_SHELLS:
            return False
    if saw_unreadable and len(pid_set) > 3:
        return None
    return True


def console_has_foreign_shell() -> bool:
    """True when a real user shell shares this console or started us."""
    if not sys.platform.startswith("win"):
        return False
    parent = (parent_process_image_name() or "").casefold()
    if parent in _FOREIGN_SHELLS:
        return True
    my_pid = os.getpid()
    for pid in console_process_ids():
        if int(pid) == my_pid:
            continue
        folded = (_process_image_name(int(pid)) or "").casefold()
        if folded in _FOREIGN_SHELLS:
            return True
    return False


def _hide_console_class_windows() -> bool:
    """Hide conhost / Windows Terminal HWNDs attached to this console."""
    if not sys.platform.startswith("win"):
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        get_pid = user32.GetWindowThreadProcessId
        get_pid.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        get_pid.restype = wintypes.DWORD
        get_class = user32.GetClassNameW
        get_class.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        get_class.restype = ctypes.c_int
        show = user32.ShowWindow
        show.argtypes = [wintypes.HWND, ctypes.c_int]
        show.restype = wintypes.BOOL
        enum_windows = user32.EnumWindows
        enum_windows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
        enum_windows.restype = wintypes.BOOL

        pids = {int(p) for p in console_process_ids()}
        pids.add(os.getpid())
        hidden = False

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _enum(hwnd, _lparam):
            nonlocal hidden
            pid = wintypes.DWORD(0)
            get_pid(hwnd, ctypes.byref(pid))
            if int(pid.value) not in pids:
                return True
            buf = ctypes.create_unicode_buffer(256)
            get_class(hwnd, buf, 256)
            if buf.value in _CONSOLE_WINDOW_CLASSES:
                show(hwnd, _SW_HIDE)
                hidden = True
            return True

        enum_windows(_enum, 0)
        return hidden
    except Exception:
        return False


def hide_self_allocated_console() -> bool:
    """Hide a console Windows made for a desktop GUI launch.

    Callers only invoke this on the GUI path. A real shell (PowerShell / cmd
    running ``argusai --gui``) is left alone. Windows Terminal / conhost that
    exist only because this is a console-subsystem exe are hidden and we
    detach so the leftover blank window can close.
    """
    if console_has_foreign_shell():
        return False
    hidden = False
    handle = console_window()
    if handle:
        try:
            import ctypes

            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(handle), _SW_HIDE)
            hidden = True
        except Exception:
            pass
    if _hide_console_class_windows():
        hidden = True
    try:
        import ctypes

        # Folder builds: we are the only client, so this closes the window.
        # OneFile: the bootstrap may keep an HWND; SW_HIDE above covers that.
        ctypes.windll.kernel32.FreeConsole()
        hidden = True
    except Exception:
        pass
    return hidden


CONSOLE_DETACHED_ENV = "ARGUSAI_CONSOLE_DETACHED"


def relaunch_without_console(args: list[str]) -> bool:
    """Restart a desktop launch on a windowless console. True → caller must exit.

    Explorer, the HKCU Run key and Start Menu shortcuts give this
    console-subsystem exe a console of its own. On Windows 11 that console is
    a Windows Terminal tab: ``ShowWindow`` cannot hide it, and closing it kills
    the OneFile bootstrap, whose child then exits after Nuitka's parent-watch
    grace period. A copy started with ``CREATE_NO_WINDOW`` has no window to
    show or close, and its own bootstrap stays alive with it.
    """
    if not sys.platform.startswith("win"):
        return False
    if os.environ.pop(CONSOLE_DETACHED_ENV, None):
        return False
    if not console_window() or console_is_self_allocated() is not True:
        return False
    try:
        import subprocess

        from argusai.core.infra.runtime_exec import self_relaunch_command, self_relaunch_env

        env = self_relaunch_env()
        env[CONSOLE_DETACHED_ENV] = "1"
        subprocess.Popen(
            self_relaunch_command(*args),
            env=env,
            cwd=os.getcwd(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)),
        )
    except Exception:
        return False
    return True


def devnull_stdin_fd() -> int | None:
    """Return a fresh fd for NUL/devnull, suitable for asyncio subprocess stdin."""
    try:
        return os.open(os.devnull, os.O_RDONLY)
    except OSError:
        return None
