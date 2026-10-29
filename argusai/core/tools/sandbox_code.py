"""Assistant tool executors split from the former monolithic module."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .common import *

# ``_session_sandbox_root`` / ``_resolve_session_output_path`` /
# ``_resolve_session_input_path`` live in ``common.py`` because every
# tool module that takes a path needs them (web_static, browser_*,
# kb_tasks, browser_attach, ...). Don't redefine them here.


def session_sandbox_status(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Report sandbox directory usage, Python .venv/pip readiness, and recent audit events."""
    from argusai.core.workspace.session_sandbox import sandbox_status

    return sandbox_status(_session_sandbox_root(api, kwargs))


async def runtime_install(
    api: Any,
    runtime: str = "",
    force: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Install or repair a verified managed toolchain under data_root/tools/runtimes.

    Accepts ``python`` / ``node`` (sandbox bases) and ``wabt`` (WASM CLI tools
    used by wasm_disassemble / wasm_decompile). WABT is not part of
    reverse_toolchain_install.
    """
    import asyncio

    from argusai.core.managed_runtimes import install_runtime
    from argusai.core.paths import PathManager

    name = str(runtime or "").strip().lower()
    if name not in {"python", "node", "wabt"}:
        return {
            "ok": False,
            "error_code": "invalid_runtime",
            "error": "runtime must be 'python', 'node', or 'wabt'",
        }
    settings = api.settings_mgr.get()
    mirrors = getattr(settings, "runtime_mirrors", {})
    if not isinstance(mirrors, dict):
        mirrors = {}
    return await asyncio.to_thread(
        install_runtime,
        name,
        tools_dir=PathManager(data_root=settings.data_root).tools_dir,
        force=bool(force),
        mirrors=mirrors,
    )


async def sandbox_pip_install(
    api: Any,
    packages: Any = "",
    upgrade: bool = False,
    timeout_seconds: int = 300,
    **kwargs: Any,
) -> dict[str, Any]:
    """Install Python packages into the current session sandbox ``.venv`` via pip.

    Prefer this over ``run_host_python_*`` for dependencies. Packages stay
    session-local and do not touch the user's Conda/system Python. Call
    ``session_sandbox_status`` first when venv/pip health is unknown.
    """
    from argusai.core.workspace.session_sandbox import sandbox_pip_install as _install

    return await _install(
        _session_sandbox_root(api, kwargs),
        packages,
        upgrade=bool(upgrade),
        timeout_seconds=_safe_int(timeout_seconds, 300, 30, 1800),
    )


def discover_shell_environments(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Discover available local shell environments without running user scripts."""
    from argusai.core.workspace.session_sandbox import discover_shell_environments as _discover

    return _discover()


def discover_python_environments(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Discover available Python interpreters without running user scripts.

    Diagnostic only — paths are for display and for ``run_host_python_*``
    escalation after sandbox execution fails. Do not pass them to
    ``run_python_snippet`` / ``run_python_script`` / shell snippets.
    """
    from argusai.core.workspace.session_sandbox import discover_python_environments as _discover

    return _discover()


def sandbox_write_file(api: Any, path: str = "", content: str = "", file_type: str = "", **kwargs: Any) -> dict[str, Any]:
    """Write a text/script file into the current assistant session file sandbox.

    Prefer ``sandbox_patch_file`` for small edits to existing files — it
    replaces an exact ``old_string`` and costs far fewer tokens than
    rewriting the whole file. Use this tool for new files or full rewrites.
    """
    from argusai.core.workspace.session_sandbox import write_file

    return write_file(_session_sandbox_root(api, kwargs), path, content, file_type)


def sandbox_patch_file(
    api: Any,
    path: str = "",
    old_string: str = "",
    new_string: str = "",
    replace_all: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Apply an exact substring replacement in a sandbox text file.

    Copy ``old_string`` verbatim from ``sandbox_read_file`` output — whitespace
    and newlines must match exactly. By default the match must be unique; set
    ``replace_all=true`` only when you intentionally want every occurrence.

    Use this instead of ``sandbox_write_file`` when changing a few lines in an
    existing script, note, or config. Falls back to ``sandbox_write_file`` for
    brand-new files.
    """
    from argusai.core.workspace.session_sandbox import patch_file

    try:
        return patch_file(
            _session_sandbox_root(api, kwargs),
            path,
            old_string,
            new_string,
            replace_all=bool(replace_all),
        )
    except ValueError as exc:
        return _sandbox_path_error(exc, tool="sandbox_patch_file")


def sandbox_read_file(
    api: Any,
    path: str = "",
    offset: int = 1,
    limit: int = 200,
    start_line: int = 0,
    end_line: int = 0,
    offset_chars: int = 0,
    max_chars: int = 20000,
    read_mode: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Read a sandbox file with strict token discipline.

    **Default (line mode):** ``offset`` + ``limit`` return a numbered line
    window (~200 lines max). Use ``content_raw`` (not the ``     5|`` prefixes)
    as ``old_string`` for ``sandbox_patch_file``. Page with ``offset=end_line+1``.

    **Char mode** (minified JS): ``read_mode='chars'`` or ``offset_chars>0`` —
    returns a raw character window via ``offset_chars`` / ``max_chars``.

    Workflow for large files: ``sandbox_stat_file`` → ``sandbox_grep_file`` →
    ``sandbox_read_file(offset=<hit line>)`` → ``sandbox_patch_file``.

    Raster images (png/jpg/webp/…) are attached for vision instead of being
    decoded as text. Host/project images should be read in place with
    ``read_text_file`` / ``vision_inspect_image(path=...)`` — do not copy
    them into the sandbox just to look.
    """
    root = _session_sandbox_root(api, kwargs)
    from argusai.core.assistant.image_io import image_read_tool_result, is_raster_image_path

    if is_raster_image_path(path):
        from argusai.core.workspace.session_sandbox import IMAGE_PREVIEW_EXTS, resolve_in_sandbox

        try:
            target = resolve_in_sandbox(root, path, allowed_exts=IMAGE_PREVIEW_EXTS)
        except ValueError as exc:
            return _sandbox_path_error(exc, tool="sandbox_read_file")
        result = image_read_tool_result(target)
        if result.get("ok"):
            try:
                result["relative_path"] = target.relative_to(Path(root).resolve()).as_posix()
            except ValueError:
                pass
        return result

    mode = str(read_mode or "").strip().lower()
    use_chars = mode == "chars" or _safe_int(offset_chars, 0, 0, 10_000_000) > 0

    if use_chars:
        from argusai.core.workspace.session_sandbox import read_file

        return read_file(
            root,
            path,
            _safe_int(max_chars, 20000, 1, 100000),
            offset_chars=_safe_int(offset_chars, 0, 0, 10_000_000),
            line_numbers=True,
        )

    from argusai.core.infra import text_io
    from argusai.core.workspace.session_sandbox import _content_raw_from_numbered, resolve_in_sandbox, ALLOWED_TEXT_EXTS

    try:
        target = resolve_in_sandbox(root, path, allowed_exts=ALLOWED_TEXT_EXTS | {".jsonl"})
        rel = target.relative_to(root).as_posix()
    except ValueError as exc:
        return _sandbox_path_error(exc, tool="sandbox_read_file")

    line_offset = _safe_int(start_line, 0, 0, 10_000_000) or _safe_int(offset, 1, 1, 10_000_000)
    line_limit = _safe_int(limit, 200, 1, 800)
    if _safe_int(end_line, 0, 0, 10_000_000) >= line_offset:
        line_limit = min(800, _safe_int(end_line, 0, 0, 10_000_000) - line_offset + 1)

    result = text_io.read_text_file(
        path=rel,
        offset=line_offset,
        limit=line_limit,
        base=str(root),
    )
    if not result.get("ok"):
        return result
    numbered = str(result.get("content") or "")
    result["relative_path"] = rel
    result["read_mode"] = "lines"
    result["content_raw"] = _content_raw_from_numbered(numbered)
    result["hint"] = (
        f"Lines {result.get('start_line')}-{result.get('end_line')} of {result.get('total_lines')}. "
        "Copy patch anchors from content_raw. For next page: offset=end_line+1."
    )
    return result


def sandbox_stat_file(api: Any, path: str = "", **kwargs: Any) -> dict[str, Any]:
    """Stat a sandbox text file before reading (size, line count, encoding).

    Use on unfamiliar files — if ``large_file`` is true, prefer
    ``sandbox_grep_file`` + a small ``sandbox_read_file`` window instead of
    reading the whole file.
    """
    from argusai.core.workspace.session_sandbox import stat_file

    try:
        return stat_file(_session_sandbox_root(api, kwargs), path)
    except ValueError as exc:
        return _sandbox_path_error(exc, tool="sandbox_stat_file")


def sandbox_grep_file(
    api: Any,
    path: str = "",
    pattern: str = "",
    mode: str = "content",
    max_matches: Any = None,
    context_chars: Any = None,
    case_insensitive: Any = False,
    regex: Any = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Grep inside one sandbox text file (search before read).

    ``mode='content'`` (default): ``{line, text}`` hits — follow with
    ``sandbox_read_file(offset=<line>, limit=...)``.

    ``mode='context'``: char windows for minified single-line files; follow
    with ``sandbox_read_file(read_mode='chars', offset_chars=...)``.

    ``mode='count'`` / ``files_with_matches`` for cheap existence checks.
    """
    from argusai.core.infra import text_io
    from argusai.core.workspace.session_sandbox import resolve_in_sandbox, ALLOWED_TEXT_EXTS

    root = _session_sandbox_root(api, kwargs)
    try:
        target = resolve_in_sandbox(root, path, allowed_exts=ALLOWED_TEXT_EXTS | {".jsonl"})
        rel = target.relative_to(root).as_posix()
    except ValueError as exc:
        return _sandbox_path_error(exc, tool="sandbox_grep_file")

    result = text_io.grep_text_file(
        pattern=str(pattern or ""),
        path=rel,
        mode=mode,
        max_matches=text_io.DEFAULT_GREP_MATCHES if max_matches is None else max_matches,
        context_chars=text_io.DEFAULT_GREP_CONTEXT if context_chars is None else context_chars,
        case_insensitive=case_insensitive,
        regex=regex,
        base=str(root),
    )
    if result.get("ok"):
        result["relative_path"] = rel
        if str(mode or "content").lower() == "content" and result.get("matches"):
            first_line = result["matches"][0].get("line")
            result["hint"] = (
                f"First hit at line {first_line}. "
                f"Call sandbox_read_file(path={rel!r}, offset={first_line}, limit=40) for context."
            )
        elif str(mode or "").lower() == "context" and result.get("matches"):
            off = result["matches"][0].get("offset")
            result["hint"] = (
                f"First hit at char offset {off}. "
                f"Call sandbox_read_file(path={rel!r}, read_mode='chars', offset_chars={max(0, int(off or 0) - 200)})."
            )
    return result


def sandbox_list_files(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List files in the current assistant session file sandbox."""
    from argusai.core.workspace.session_sandbox import list_files

    return list_files(_session_sandbox_root(api, kwargs))


def sandbox_open_file(api: Any, path: str = "", **kwargs: Any) -> dict[str, Any]:
    """Open a sandbox file in the user's default OS application.

    Use this when the user asks "open this file", "show me the report",
    "view this PoC in the browser" and the file already lives in the session
    sandbox. The path must be sandbox-scoped (relative to the sandbox root
    or an absolute path that's inside it); extensions are limited to a
    safe-to-preview allowlist (text, markdown, HTML, images, PDF) so the
    OS shell never gets asked to run a script or executable. The application
    window opens out-of-process — control returns immediately and the AI
    does not wait for the user to close the file.
    """
    from argusai.core.workspace.session_sandbox import open_file

    return open_file(_session_sandbox_root(api, kwargs), path)


def sandbox_reveal_in_folder(api: Any, path: str = "", **kwargs: Any) -> dict[str, Any]:
    """Open the sandbox folder (or the given file's folder) in the OS file manager.

    Pass an empty ``path`` to open the sandbox root itself. Useful when the
    user says "show me where the files are" or wants to inspect a generated
    artifact alongside its neighbours.
    """
    from argusai.core.workspace.session_sandbox import reveal_in_folder

    return reveal_in_folder(_session_sandbox_root(api, kwargs), path)


def sandbox_delete_file(api: Any, path: str = "", **kwargs: Any) -> dict[str, Any]:
    """Delete a file from the current assistant session file sandbox."""
    from argusai.core.workspace.session_sandbox import delete_file

    return delete_file(_session_sandbox_root(api, kwargs), path)


def sandbox_reset_files(api: Any, confirm_label: str = "", **kwargs: Any) -> dict[str, Any]:
    """Reset files in the current assistant session file sandbox."""
    from argusai.core.workspace.session_sandbox import reset_sandbox

    if str(confirm_label or "").strip() != "RESET":
        return {"ok": False, "error": "confirm_label must be RESET"}
    return reset_sandbox(_session_sandbox_root(api, kwargs))


def _sandbox_path_error(exc: ValueError, *, tool: str) -> dict[str, Any]:
    """Map a ``resolve_in_sandbox`` ValueError to a Loom22 §4.4.5 envelope.

    The dispatch layer turns raw ValueError into ``{"error": str(e)}``,
    which is opaque enough that the model retries with a "smarter"
    shell snippet (defeating the boundary check). Catching the
    boundary errors locally lets us hand back an actionable
    ``error_code`` / ``suggested_fix`` instead.
    """
    msg = str(exc)
    if "escapes sandbox" in msg:
        code = "path_outside_sandbox"
        fix = (
            "The path must live inside the current session sandbox. "
            "Pick ONE of:\n"
            "  1. sandbox_write_file(path='<name>.py', content=<file body>) "
            "to copy the script in, then call this tool with the returned "
            "relative_path.\n"
            "  2. run_python_snippet(code=<inline code>) when the logic is "
            "short enough to embed.\n"
            "Going through run_shell_snippet does NOT help — that shell also "
            "runs inside the sandbox cwd with the sandbox venv on PATH."
        )
    elif "absolute paths are not allowed" in msg or "path traversal" in msg or "hidden paths" in msg:
        code = "invalid_sandbox_path"
        fix = (
            "Pass a sandbox-relative path (e.g. 'analysis/probe.py'). "
            "No '..', no drive letters, no leading dots."
        )
    elif "extension not allowed" in msg:
        code = "unsupported_extension"
        fix = (
            "Only .py / .ps1 / .bat / .cmd / .sh are runnable in the sandbox. "
            "Rewrite the logic in one of those (Python is preferred)."
        )
    else:
        code = "invalid_path"
        fix = "Check the path; it failed sandbox validation."
    return {
        "ok": False,
        "error": msg,
        "error_code": code,
        "retryable": False,
        "suggested_fix": fix,
        "tool": tool,
    }


async def run_python_script(
    api: Any,
    path: str = "",
    python_env: str = "",
    args: list[str] | str | None = None,
    timeout_seconds: int = 30,
    background: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a Python script that already lives INSIDE the current session sandbox.

    When to use: you previously wrote the script via sandbox_write_file,
    run_python_snippet (which persists a snippet under .tmp/), or a tool
    that materialised a PoC into the sandbox (e.g. create_python_poc,
    post_exploit_lab_generate).

    When NOT to use: the script lives anywhere else on disk. ``path`` is
    sandbox-relative (e.g. ``probe.py``, ``analysis/dump.py``) or an
    absolute path that resolves into the sandbox root. Paths outside the
    sandbox — the repo, the user's home, or ``C:\\Windows`` — are REJECTED with
    ``error_code=path_outside_sandbox``. Going through run_shell_snippet
    does NOT bypass this, the shell also runs in the sandbox cwd.

    What to do instead when the script lives outside the sandbox:
      1. sandbox_write_file the source into the sandbox first, then call
         this tool with the returned ``relative_path``.
      2. For one-off logic, run_python_snippet(code=...) is simpler.

    Execution environment: the script runs from the sandbox cwd inside
    the sandbox's own ``.venv``. Host-side site-packages are NOT visible;
    install dependencies with pip inside the sandbox first if needed.
    Do not pass ``python_env`` — use ``run_host_python_script`` when the
    sandbox .venv truly cannot satisfy the task.

    Missing managed Python is handled by the sandbox-environment confirmation
    card or by ``runtime_install``; this tool never uses host Python silently.

    timeout_seconds: 1-1800 (default 30). On timeout the process is
    killed but partial stdout/stderr is still returned with
    ``timed_out=true`` and ``partial_output=true``.

    background: when true, detach after a short settle window and return
    ``ok=true, detached=true, pid=...`` without waiting for exit. Use this
    for GUI apps / long-lived servers so the default timeout does not kill
    a process that already started successfully.
    """
    from argusai.core.workspace.session_sandbox import run_python_script as _run

    try:
        return await _run(
            _session_sandbox_root(api, kwargs),
            path,
            python_env=python_env,
            args=args,
            timeout_seconds=_safe_int(timeout_seconds, 30, 1, 1800),
            background=_safe_bool(background, False),
        )
    except ValueError as exc:
        return _sandbox_path_error(exc, tool="run_python_script")


async def run_shell_script(
    api: Any,
    path: str = "",
    shell: str = "",
    args: list[str] | str | None = None,
    timeout_seconds: int = 30,
    background: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a shell script (.ps1/.cmd/.bat/.sh) that lives INSIDE the current session sandbox.

    Same boundary rules as run_python_script: the file must already be in
    the sandbox; write it via sandbox_write_file first if it isn't. The
    script runs from the sandbox cwd. If it invokes ``python``/``pip``,
    those PATH entries are auto-rewired to the sandbox venv (NOT the
    host's Python install). Hard-coding a host interpreter path in the
    script body is blocked — use ``run_host_python_script`` instead.

    Missing managed Python is installed only after explicit confirmation.

    timeout_seconds: 1-1800 (default 30). On timeout the process is
    killed but partial output is returned.

    background: detach after settle for GUI/servers (see run_python_script).
    """
    from argusai.core.workspace.session_sandbox import run_shell_script as _run

    resolved_shell = str(shell or "").strip()
    if not resolved_shell:
        try:
            from argusai.core.settings import normalize_default_shell

            pref = normalize_default_shell(
                getattr(api.settings_mgr.get(), "assistant_default_shell", "auto")
            )
            if pref not in {"", "auto"}:
                resolved_shell = pref
        except Exception:
            pass
    try:
        return await _run(
            _session_sandbox_root(api, kwargs),
            path,
            shell=resolved_shell,
            args=args,
            timeout_seconds=_safe_int(timeout_seconds, 30, 1, 1800),
            background=_safe_bool(background, False),
        )
    except ValueError as exc:
        return _sandbox_path_error(exc, tool="run_shell_script")


async def run_python_snippet(
    api: Any,
    code: str = "",
    python_env: str = "",
    args: list[str] | str | None = None,
    timeout_seconds: int = 30,
    background: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Write and run an inline Python snippet inside the current session sandbox.

    Preferred over run_python_script for one-off logic — the snippet is
    materialised as ``.tmp/snippet-*.py`` under the sandbox root and
    executed with the sandbox venv. Boundary is the same: the snippet
    body cannot reach files outside the sandbox unless the user has
    explicitly dropped them in. Do not pass ``python_env`` or embed host
    interpreter paths — use ``run_host_python_snippet`` for host escalation.

    Use this — not run_shell_snippet — for arbitrary Python computation.
    Reach for run_shell_snippet only when you genuinely need shell
    syntax (pipes, env vars, native binaries that don't have a Python
    equivalent).

    timeout_seconds: 1-1800 (default 30). For long-running tasks (SQLi
    blind, crawls, brute force) set timeout_seconds to a few hundred;
    on timeout you still get the partial stdout/stderr captured before
    kill.

    background: detach after settle for GUI/servers so timeout does not
    kill a successfully started process.
    """
    from argusai.core.workspace.session_sandbox import run_python_snippet as _run

    try:
        return await _run(
            _session_sandbox_root(api, kwargs),
            code,
            python_env=python_env,
            args=args,
            timeout_seconds=_safe_int(timeout_seconds, 30, 1, 1800),
            background=_safe_bool(background, False),
        )
    except ValueError as exc:
        return _sandbox_path_error(exc, tool="run_python_snippet")


async def run_shell_snippet(
    api: Any,
    script: str = "",
    shell: str = "",
    timeout_seconds: int = 30,
    background: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Write and run an inline shell snippet inside the current session sandbox.

    The body is saved as ``.tmp/snippet-*.{ps1,cmd,sh}`` and executed
    with the sandbox root as ``cwd``. Default ``shell`` is PowerShell on
    Windows / bash on POSIX. Use ``discover_shell_environments`` first
    if you need to enumerate available shells.

    Command chaining on Windows: the default ``powershell`` is usually
    Windows PowerShell 5.1, which does NOT support ``&&`` / ``||`` (they
    raise a ParserError). Either chain with ``;`` / write separate calls,
    or pass ``shell="cmd"`` (cmd supports ``&&`` / ``||``). The runner now
    also auto-falls-back: a PowerShell snippet using ``&&`` / ``||`` is
    redirected to ``pwsh`` if installed, else to ``cmd``, and the result
    carries a ``shell_auto_switch`` note when that happens.

    PATH semantics — read this before you reach for shell tricks: the
    sandbox venv's ``Scripts`` / ``bin`` dir is on PATH FIRST. So a bare
    ``python script.py`` runs the SANDBOX venv (a fresh, mostly empty
    interpreter), NOT the host Python that launched ArgusAI. Hard-coding
    an absolute host ``python.exe`` path or using ``py -3`` is blocked.
    For host Python, call ``run_host_python_snippet`` / ``run_host_python_script``
    after sandbox execution fails. Tunnelling through shell does NOT let
    you reach files outside the sandbox either — paths still resolve under
    the sandbox cwd.

    Do not use this tool to install runtimes. Missing Python/Node bases are
    handled by the managed sandbox-runtime confirmation gate.

    Prefer run_python_snippet for portable text/file/data processing.
    Use this only when you actually need shell syntax.

    timeout_seconds: 1-1800 (default 30). On timeout the partial output
    is still returned.

    background: for GUI apps / servers set true so the runner returns
    ``detached=true`` after a short settle window instead of waiting until
    timeout and killing the child (a common false "launch failed" signal).
    """
    from argusai.core.workspace.session_sandbox import run_shell_snippet as _run

    default_shell = "auto"
    try:
        default_shell = str(getattr(api.settings_mgr.get(), "assistant_default_shell", "auto") or "auto")
    except Exception:
        pass
    try:
        return await _run(
            _session_sandbox_root(api, kwargs),
            script,
            shell=shell,
            timeout_seconds=_safe_int(timeout_seconds, 30, 1, 1800),
            background=_safe_bool(background, False),
            default_shell=default_shell,
        )
    except ValueError as exc:
        return _sandbox_path_error(exc, tool="run_shell_snippet")


async def run_host_python_script(
    api: Any,
    path: str = "",
    reason: str = "",
    sandbox_attempt_summary: str = "",
    python_env: str = "",
    args: list[str] | str | None = None,
    timeout_seconds: int = 30,
    background: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a sandbox-resident ``.py`` file with a HOST Python interpreter (explicit escalation).

    Use ONLY after ``run_python_script`` / ``run_python_snippet`` in the
    session sandbox has failed for a capability the sandbox .venv cannot
    provide (e.g. tkinter/Tcl/Tk bound to host stdlib, not pip-installable).

    Requires ``reason`` and ``sandbox_attempt_summary`` so the confirmation
    card shows why sandbox execution was insufficient. Always needs user
    approval (never auto-allow). Optional ``python_env`` may be an absolute
    path or name from ``discover_python_environments``; empty uses the best
    available host interpreter.

    The script file must still live inside the session sandbox; only the
    interpreter is host-side. cwd remains the sandbox root.

    background: detach for host-side GUI apps (Flet/Tk/etc.) so timeout
    does not kill a window that already opened.
    """
    from argusai.core.workspace.session_sandbox import run_host_python_script as _run

    try:
        return await _run(
            _session_sandbox_root(api, kwargs),
            path,
            reason=reason,
            sandbox_attempt_summary=sandbox_attempt_summary,
            python_env=python_env,
            args=args,
            timeout_seconds=_safe_int(timeout_seconds, 30, 1, 1800),
            background=_safe_bool(background, False),
        )
    except ValueError as exc:
        return _sandbox_path_error(exc, tool="run_host_python_script")


async def run_host_python_snippet(
    api: Any,
    code: str = "",
    reason: str = "",
    sandbox_attempt_summary: str = "",
    python_env: str = "",
    args: list[str] | str | None = None,
    timeout_seconds: int = 30,
    background: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Write and run inline Python with a HOST interpreter (explicit escalation).

    Same rules as ``run_host_python_script``, but materialises ``code`` as
    ``.tmp/host-snippet-*.py`` under the sandbox audit tree first. Prefer
    ``run_python_snippet`` for all normal probes; reach for this tool only
    when sandbox execution has already failed with evidence.
    """
    from argusai.core.workspace.session_sandbox import run_host_python_snippet as _run

    try:
        return await _run(
            _session_sandbox_root(api, kwargs),
            code,
            reason=reason,
            sandbox_attempt_summary=sandbox_attempt_summary,
            python_env=python_env,
            args=args,
            timeout_seconds=_safe_int(timeout_seconds, 30, 1, 1800),
            background=_safe_bool(background, False),
        )
    except ValueError as exc:
        return _sandbox_path_error(exc, tool="run_host_python_snippet")


def _shell_session_assistant_id(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("_session_id") or "default")


def _shell_session_env(root: Path) -> dict[str, str] | None:
    from argusai.core.workspace.session_sandbox import PY_VENV_DIR, _venv_env

    if (root / PY_VENV_DIR).exists():
        return _venv_env(root)
    return None


def shell_session_start(
    api: Any,
    shell: str = "",
    initial_command: str = "",
    label: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Start an interactive shell session (stdin-pipe, not a full PTY).

    Returns ``session_id`` immediately. Send further commands with
    ``shell_session_exec``, poll output with ``shell_session_read``, and end
    with ``shell_session_stop``. Prefer this over blocking ``run_shell_snippet``
    for multi-step work, servers, or GUI launches (use Start-Process /
    ``background=true`` oneshot so the shell is not blocked by a GUI).
    """
    from argusai.core.workspace.session_sandbox import _dangerous_shell_reason
    from argusai.core.workspace.shell_sessions import get_shell_session_manager

    root = Path(_session_sandbox_root(api, kwargs))
    return get_shell_session_manager().start_interactive(
        sandbox_root=root,
        assistant_session_id=_shell_session_assistant_id(kwargs),
        shell=shell,
        initial_command=initial_command,
        label=label,
        env=_shell_session_env(root),
        dangerous_check=_dangerous_shell_reason,
    )


def shell_session_exec(
    api: Any,
    session_id: str = "",
    command: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Send one command line to an interactive shell session stdin."""
    from argusai.core.workspace.session_sandbox import _dangerous_shell_reason
    from argusai.core.workspace.shell_sessions import get_shell_session_manager

    return get_shell_session_manager().exec_command(
        str(session_id or "").strip(),
        command,
        assistant_session_id=_shell_session_assistant_id(kwargs),
        dangerous_check=_dangerous_shell_reason,
    )


def shell_session_write(
    api: Any,
    session_id: str = "",
    data: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Write raw bytes/text to an interactive shell session stdin (multi-line OK)."""
    from argusai.core.workspace.session_sandbox import _dangerous_shell_reason
    from argusai.core.workspace.shell_sessions import get_shell_session_manager

    return get_shell_session_manager().write_raw(
        str(session_id or "").strip(),
        data,
        assistant_session_id=_shell_session_assistant_id(kwargs),
        dangerous_check=_dangerous_shell_reason,
    )


def shell_session_list(api: Any, **kwargs: Any) -> dict[str, Any]:
    """List shell sessions for the current assistant chat session."""
    from argusai.core.workspace.shell_sessions import get_shell_session_manager

    sessions = get_shell_session_manager().list_sessions(_shell_session_assistant_id(kwargs))
    return {"ok": True, "count": len(sessions), "sessions": sessions}


def shell_session_read(
    api: Any,
    session_id: str = "",
    offset: int = 0,
    max_chars: int = 12000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read incremental stdout/stderr log from a shell session."""
    from argusai.core.workspace.shell_sessions import get_shell_session_manager

    return get_shell_session_manager().read(
        str(session_id or "").strip(),
        assistant_session_id=_shell_session_assistant_id(kwargs),
        offset=_safe_int(offset, 0, 0, 10_000_000),
        max_chars=_safe_int(max_chars, 12000, 1, 80000),
    )


def shell_session_stop(api: Any, session_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """Stop a shell session and kill its process tree."""
    from argusai.core.workspace.shell_sessions import get_shell_session_manager

    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": False, "error": "session_id is required", "error_code": "invalid_arguments"}
    return get_shell_session_manager().stop(
        sid,
        assistant_session_id=_shell_session_assistant_id(kwargs),
    )


async def whois_lookup(api: Any, target: str = "", scope: str = "", **kwargs: Any) -> dict[str, Any]:
    """Run scoped whois lookup for an authorized target."""
    from argusai.core.security.network_tools import whois_lookup as _whois_lookup

    return await _whois_lookup(target, scope=scope)


async def nmap_scan(
    api: Any,
    target: str = "",
    scope: str = "",
    ports: str = "top-100",
    service_detection: bool = False,
    aggressive: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run scoped nmap scan for an authorized target."""
    from argusai.core.security.network_tools import nmap_scan as _nmap_scan

    return await _nmap_scan(
        target,
        scope=scope,
        ports=ports,
        service_detection=service_detection,
        aggressive=aggressive,
    )


async def network_discover_hosts(
    api: Any,
    target: str = "",
    scope: str = "",
    max_hosts: int = 64,
    **kwargs: Any,
) -> dict[str, Any]:
    """Resolve or enumerate authorized network targets before deeper probing."""
    from ipaddress import ip_network
    from argusai.core.security.network_recon import host_in_scope, normalize_host, normalize_scope, resolve_host

    scope_list = normalize_scope(scope)
    raw = (target or "").strip()
    if not raw:
        return {"ok": False, "error": "target is required"}
    if "/" in raw and "://" not in raw:
        try:
            net = ip_network(raw, strict=False)
        except ValueError as e:
            return {"ok": False, "error": f"invalid CIDR: {e}"}
        limit = _safe_int(max_hosts, 64, 1, 256)
        hosts = [str(ip) for idx, ip in enumerate(net.hosts()) if idx < limit and host_in_scope(str(ip), scope_list)]
        return {"ok": True, "mode": "cidr", "target": raw, "scope": scope_list, "count": len(hosts), "hosts": hosts}
    host = normalize_host(raw)
    if not host_in_scope(host, scope_list):
        return {"ok": False, "error": f"target out of authorized scope: {host}", "scope": scope_list}
    return {"ok": True, "mode": "resolve", "target": host, "scope": scope_list, "hosts": [{"host": host, "ips": resolve_host(host)}]}


async def network_scan_ports(
    api: Any,
    target: str = "",
    scope: str = "",
    ports: str = "common",
    timeout_seconds: int = 3,
    concurrency: int = 64,
    max_ports: int = 256,
    **kwargs: Any,
) -> dict[str, Any]:
    """Scan authorized TCP ports and return open service fingerprints."""
    from argusai.core.security.network_recon import scan_ports

    return await scan_ports(
        target,
        scope,
        ports=ports,
        timeout_seconds=float(_safe_int(timeout_seconds, 3, 1, 30)),
        concurrency=_safe_int(concurrency, 64, 1, 256),
        max_ports=_safe_int(max_ports, 256, 1, 1024),
    )


async def network_fingerprint_services(
    api: Any,
    target: str = "",
    scope: str = "",
    ports: str = "common",
    timeout_seconds: int = 3,
    **kwargs: Any,
) -> dict[str, Any]:
    """Fingerprint services on authorized ports with lightweight protocol probes."""
    return await network_scan_ports(
        api,
        target=target,
        scope=scope,
        ports=ports,
        timeout_seconds=timeout_seconds,
        concurrency=_safe_int(kwargs.get("concurrency"), 48, 1, 128),
        max_ports=_safe_int(kwargs.get("max_ports"), 256, 1, 1024),
    )


async def network_profile_host(
    api: Any,
    target: str = "",
    scope: str = "",
    ports: str = "common",
    timeout_seconds: int = 3,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build an authorized host profile with open ports, services, OS hints, and exposure findings."""
    from argusai.core.security.network_recon import infer_os

    result = await network_scan_ports(
        api,
        target=target,
        scope=scope,
        ports=ports,
        timeout_seconds=timeout_seconds,
        concurrency=_safe_int(kwargs.get("concurrency"), 64, 1, 256),
        max_ports=_safe_int(kwargs.get("max_ports"), 256, 1, 1024),
    )
    if result.get("ok") and isinstance(result.get("profile"), dict):
        result["profile"]["os"] = infer_os(result["profile"])
    return result


def network_assess_exposure(api: Any, profile: Any = None, profile_json: str = "", **kwargs: Any) -> dict[str, Any]:
    """Assess a network host profile for non-destructive exposure findings."""
    import json
    from argusai.core.security.network_recon import assess_exposure, infer_os

    data = profile if isinstance(profile, dict) else {}
    if profile_json:
        try:
            raw = json.loads(profile_json)
            data = raw.get("profile", raw) if isinstance(raw, dict) else {}
        except Exception as e:
            return {"ok": False, "error": f"invalid profile_json: {e}"}
    if not data:
        return {"ok": False, "error": "profile or profile_json is required"}
    findings = assess_exposure(data)
    os_hints = infer_os(data)
    return {"ok": True, "target": data.get("host", ""), "findings": findings, "os": os_hints, "finding_count": len(findings)}


def network_protocol_assess(api: Any, profile: Any = None, profile_json: str = "", **kwargs: Any) -> dict[str, Any]:
    """Assess protocol-specific exposure risks from a host profile without active exploitation."""
    import json
    from argusai.core.security.network_recon import assess_protocols

    data = profile if isinstance(profile, dict) else {}
    if profile_json:
        try:
            raw = json.loads(profile_json)
            data = raw.get("profile", raw) if isinstance(raw, dict) else {}
        except Exception as e:
            return {"ok": False, "error": f"invalid profile_json: {e}"}
    if not data:
        return {"ok": False, "error": "profile or profile_json is required"}
    return assess_protocols(data)


def vuln_intel_match(api: Any, profile: Any = None, profile_json: str = "", fingerprint_json: str = "", **kwargs: Any) -> dict[str, Any]:
    """Match CPE/product/version evidence to offline CVE risk candidates."""
    import json
    from argusai.core.security.vuln_intel import match_profile, match_service_vulns

    if fingerprint_json:
        try:
            fp = json.loads(fingerprint_json)
        except Exception as e:
            return {"ok": False, "error": f"invalid fingerprint_json: {e}"}
        return match_service_vulns(fp if isinstance(fp, dict) else {})
    data = profile if isinstance(profile, dict) else {}
    if profile_json:
        try:
            raw = json.loads(profile_json)
            data = raw.get("profile", raw) if isinstance(raw, dict) else {}
        except Exception as e:
            return {"ok": False, "error": f"invalid profile_json: {e}"}
    if not data:
        return {"ok": False, "error": "profile_json or fingerprint_json is required"}
    return match_profile(data)


def weak_credential_audit_plan(api: Any, service: str = "", profile_json: str = "", **kwargs: Any) -> dict[str, Any]:
    """Plan a controlled weak-credential audit without attempting login."""
    import json
    from argusai.core.security.weak_credentials import weak_credential_plan

    profile = {}
    if profile_json:
        try:
            raw = json.loads(profile_json)
            profile = raw.get("profile", raw) if isinstance(raw, dict) else {}
        except Exception as e:
            return {"ok": False, "error": f"invalid profile_json: {e}"}
    return weak_credential_plan(service=service, profile=profile)


async def weak_credential_check(
    api: Any,
    service: str = "",
    target: str = "",
    scope: str = "",
    credentials_json: str = "",
    port: int = 0,
    url: str = "",
    use_default_candidates: bool = False,
    max_attempts: int = 6,
    delay_seconds: int = 2,
    timeout_seconds: int = 5,
    stop_on_success: bool = True,
    authorization_phrase: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a controlled low-rate weak-credential audit for an authorized target."""
    import json
    from argusai.core.security.weak_credentials import weak_credential_check as _check

    credentials = []
    if credentials_json:
        try:
            raw = json.loads(credentials_json)
            credentials = raw if isinstance(raw, list) else [raw]
        except Exception as e:
            return {"ok": False, "error": f"invalid credentials_json: {e}"}
    return await _check(
        service=service,
        target=target,
        scope=scope,
        credentials=[c for c in credentials if isinstance(c, dict)],
        port=_safe_int(port, 0, 0, 65535),
        url=url,
        use_default_candidates=bool(use_default_candidates),
        max_attempts=_safe_int(max_attempts, 6, 1, 10),
        delay_seconds=float(_safe_int(delay_seconds, 2, 1, 60)),
        timeout_seconds=float(_safe_int(timeout_seconds, 5, 1, 30)),
        stop_on_success=bool(stop_on_success),
        authorization_phrase=authorization_phrase,
    )


def pentest_plan_chain(
    api: Any,
    target: str = "",
    scope: str = "",
    objective: str = "authorized validation",
    rules_json: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan a scoped authorized pentest chain without executing high-risk steps."""
    import json
    from argusai.core.security.network_recon import build_pentest_chain

    rules: dict[str, Any] = {}
    if rules_json:
        try:
            parsed = json.loads(rules_json)
            rules = parsed if isinstance(parsed, dict) else {}
        except Exception as e:
            return {"ok": False, "error": f"invalid rules_json: {e}"}
    return build_pentest_chain(target, scope, objective=objective, rules_of_engagement=rules)


def pentest_verify_finding(
    api: Any,
    target: str = "",
    scope: str = "",
    finding: Any = None,
    finding_json: str = "",
    authorization_phrase: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Minimally verify one authorized finding; requires authorization_phrase=AUTHORIZED."""
    import json
    from argusai.core.security.network_recon import verify_finding_minimal

    data = finding if isinstance(finding, dict) else {}
    if finding_json:
        try:
            raw = json.loads(finding_json)
            data = raw if isinstance(raw, dict) else {}
        except Exception as e:
            return {"ok": False, "error": f"invalid finding_json: {e}"}
    if not data:
        return {"ok": False, "error": "finding or finding_json is required"}
    return verify_finding_minimal(data, target, scope, authorization_phrase=authorization_phrase)


def post_exploit_map_impact(
    api: Any,
    target: str = "",
    findings_json: str = "",
    profile_json: str = "",
    evidence_refs: list[str] | str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Map post-exploitation impact hypotheses from findings and host profile without executing actions."""
    import json
    from argusai.core.security.post_exploit import map_impact

    try:
        findings_raw = json.loads(findings_json or "[]")
        profile = json.loads(profile_json or "{}")
    except Exception as e:
        return {"ok": False, "error": f"invalid JSON input: {e}"}
    refs = evidence_refs
    if isinstance(refs, str):
        refs = [x.strip() for x in refs.split(",") if x.strip()]
    return map_impact(target, findings_raw if isinstance(findings_raw, list) else [], profile if isinstance(profile, dict) else {}, refs or [])


def post_exploit_plan_actions(api: Any, assessment_json: str = "", mode: str = "real_target", **kwargs: Any) -> dict[str, Any]:
    """Plan safe next actions from a post-exploitation assessment."""
    import json
    from argusai.core.security.post_exploit import plan_actions

    try:
        raw = json.loads(assessment_json or "{}")
    except Exception as e:
        return {"ok": False, "error": f"invalid assessment_json: {e}"}
    assessment = raw.get("assessment", raw) if isinstance(raw, dict) else {}
    return plan_actions(assessment, mode=mode)


def post_exploit_lab_plan(api: Any, finding_json: str = "", profile_json: str = "", **kwargs: Any) -> dict[str, Any]:
    """Plan a sandbox-only local harness for demonstrating detection logic."""
    import json
    from argusai.core.security.post_exploit import lab_plan

    try:
        finding = json.loads(finding_json or "{}")
        profile = json.loads(profile_json or "{}")
    except Exception as e:
        return {"ok": False, "error": f"invalid JSON input: {e}"}
    return lab_plan(finding if isinstance(finding, dict) else {}, profile if isinstance(profile, dict) else {})


def post_exploit_lab_generate(api: Any, finding_json: str = "", profile_json: str = "", **kwargs: Any) -> dict[str, Any]:
    """Write a sandbox-only post-exploitation lab harness; it does not connect to real targets."""
    import json
    from argusai.core.security.post_exploit import lab_harness_files, lab_plan
    from argusai.core.workspace.session_sandbox import write_file

    try:
        finding = json.loads(finding_json or "{}")
        profile = json.loads(profile_json or "{}")
    except Exception as e:
        return {"ok": False, "error": f"invalid JSON input: {e}"}
    finding_d = finding if isinstance(finding, dict) else {}
    profile_d = profile if isinstance(profile, dict) else {}
    root = _session_sandbox_root(api, kwargs)
    files = []
    for path, content in lab_harness_files(finding_d, profile_d).items():
        files.append(write_file(root, path, content, "post-exploit-lab"))
    return {
        "ok": True,
        "lab_plan": lab_plan(finding_d, profile_d),
        "files": files,
        "boundary": "sandbox_lab_only",
        "note": "Generated files do not contact real targets; run only inside the session sandbox.",
    }


# === Analysis executors ===

def analyze_findings(api: Any, task_id: str = "", severity: str = "", **kwargs: Any) -> dict[str, Any]:
    task = api.get_task(task_id)
    if not task:
        return {"error": f"Task not found: {task_id}"}
    findings = task.get("findings", [])
    if severity:
        findings = [f for f in findings if f.get("severity") == severity]

    order = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    sorted_findings = sorted(findings, key=lambda x: order.get(x.get("severity", "info"), 0), reverse=True)

    counts: dict[str, int] = {}
    categories: dict[str, int] = {}
    for f in findings:
        s = f.get("severity", "info")
        counts[s] = counts.get(s, 0) + 1
        cat = f.get("id", "").split(".")[0] if "." in f.get("id", "") else f.get("id", "")
        categories[cat] = categories.get(cat, 0) + 1

    return {
        "task_id": task_id,
        "total_findings": len(findings),
        "severity_counts": counts,
        "categories": categories,
        "top_critical": [
            {"uid": f.get("uid"), "id": f.get("id"), "title": f.get("title"),
             "severity": f.get("severity"), "url": f.get("url"),
             "cwe": f.get("cwe"), "remediation": f.get("remediation", "")[:200]}
            for f in sorted_findings if f.get("severity") in ("critical", "high")
        ][:5],
        "all_titles": [
            {"severity": f.get("severity"), "id": f.get("id"), "title": f.get("title")}
            for f in sorted_findings
        ],
    }


# ----------------------------------------------------------------------
# Generic text I/O — two-step read + grep primitives.
#
# These exist so the model can ask "where in this 5 MB file is function
# X" without our HTTP layer streaming the whole file into the prompt.
# Pair them with one another:
#
#   grep_text(pattern, path) → path:line:match records (~2 KB)
#   read_text_file(path, offset, limit) → just the lines you want (~5 KB)
#
# Every payload here is hard-capped server-side, in addition to the
# session-level shrinker in ``assistant.py``.
# ----------------------------------------------------------------------


def read_text_file(
    api: Any,
    path: str = "",
    offset: Any = 1,
    limit: Any = None,
    encoding: str = "",
    line_trunc: Any = None,
    read_mode: str = "",
    offset_chars: int = 0,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read a slice of a user/system text file with strict token discipline.

    **Default (line mode):** ``offset`` + ``limit`` return numbered lines plus
    ``content_raw`` for ``patch_text_file``. Page with ``offset=end_line+1``.

    **Char mode** (minified JS): ``read_mode='chars'`` or ``offset_chars>0`` —
    delegates to ``read_text_slice`` (raw character window).

    Workflow for large files: ``stat_text_file`` → ``grep_text`` →
    ``read_text_file(offset=<hit line>)`` → ``patch_text_file``.

    Raster images are attached for vision in place (any host path). Do not
    copy them into the session sandbox just to look.
    """
    from argusai.core.assistant.image_io import image_read_tool_result, is_raster_image_path
    from argusai.core.infra import text_io

    if is_raster_image_path(path):
        return image_read_tool_result(path)

    mode = str(read_mode or "").strip().lower()
    use_chars = mode == "chars" or _safe_int(offset_chars, 0, 0, 10_000_000) > 0
    if use_chars:
        result = text_io.read_text_slice(
            path=path,
            offset_chars=offset_chars,
            length=text_io.DEFAULT_SLICE_LENGTH,
            encoding=(encoding or None),
            base=None,
        )
        if result.get("ok"):
            result["read_mode"] = "chars"
            result["content_raw"] = str(result.get("content") or "")
        return result

    return text_io.read_text_file(
        path=path,
        offset=offset,
        limit=text_io.DEFAULT_LIMIT_LINES if limit is None else limit,
        encoding=(encoding or None),
        line_trunc=text_io.DEFAULT_LINE_TRUNC if line_trunc is None else line_trunc,
        base=None,
    )


def stat_text_file(api: Any, path: str = "", **kwargs: Any) -> dict[str, Any]:
    """Cheap metadata-only probe before reading user/system files.

    Returns size, line count, encoding hint, and ``large_file``. Use this first
    when you don't know how big a file is — if it's huge, call ``grep_text`` to
    find the interesting region rather than blindly ``read_text_file``.
    """
    from argusai.core.infra import text_io

    return text_io.stat_text_file(path=path, base=None)


def patch_text_file(
    api: Any,
    path: str = "",
    old_string: str = "",
    new_string: str = "",
    replace_all: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Apply an exact substring replacement on a user/system text file.

    Copy ``old_string`` verbatim from ``read_text_file.content_raw`` — whitespace
    and newlines must match exactly. By default the match must be unique; set
    ``replace_all=true`` only when you intentionally want every occurrence.

    Use this instead of ``write_text_file`` when changing a few lines in an
    existing file on disk. Requires user confirmation.
    """
    from argusai.core.infra import text_io

    return text_io.patch_text_file(
        path=path,
        old_string=old_string,
        new_string=new_string,
        replace_all=bool(replace_all),
        base=None,
    )


def write_text_file(api: Any, path: str = "", content: str = "", **kwargs: Any) -> dict[str, Any]:
    """Create or fully replace a user/system text file.

    Prefer ``patch_text_file`` for small edits to existing files — it costs far
    fewer tokens than rewriting the whole file. Requires user confirmation.
    """
    from argusai.core.infra import text_io

    return text_io.write_text_file(path=path, content=content, base=None)


def grep_text(
    api: Any,
    pattern: str = "",
    path: str = "",
    mode: str = "content",
    max_matches: Any = None,
    line_trunc: Any = None,
    context_chars: Any = None,
    case_insensitive: Any = False,
    regex: Any = True,
    encoding: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Grep a single text file (ripgrep-style).

    Use this BEFORE ``read_text_file`` when you don't know which part of
    a large file is relevant. Returns:

    - ``mode='content'`` (default): ``{line, text}`` records — line-oriented.
    - ``mode='context'``: ``{offset, end, before, match, after}`` —
      CHARACTER-oriented, walks past newlines. **Use this for single-line
      minified JS** where the entire file is "line 1" and the regular
      content mode is useless.
    - ``mode='files_with_matches'``: ``{matched: bool}``.
    - ``mode='count'``: ``{count: int}``.

    Pattern is regex by default; pass ``regex=false`` for literal text.
    ``context_chars`` (default 200, max 2000) controls the window size
    in ``mode='context'``.
    """
    from argusai.core.infra import text_io

    return text_io.grep_text_file(
        pattern=str(pattern or ""),
        path=str(path or ""),
        mode=mode,
        max_matches=text_io.DEFAULT_GREP_MATCHES if max_matches is None else max_matches,
        line_trunc=text_io.MAX_GREP_LINE_LEN if line_trunc is None else line_trunc,
        context_chars=text_io.DEFAULT_GREP_CONTEXT if context_chars is None else context_chars,
        case_insensitive=case_insensitive,
        regex=regex,
        encoding=(encoding or None),
        base=None,
    )


def read_text_slice(
    api: Any,
    path: str = "",
    offset_chars: Any = 0,
    length: Any = None,
    encoding: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Read a CHARACTER-OFFSET slice of a text file (the "awk for
    minified files" primitive).

    Use this when ``read_text_file`` is useless because the file is one
    giant line — typical for minified JS bundles, compacted JSON, base64
    blobs. Stat the file first to get ``size_bytes`` / ``total_chars``,
    then walk windows of ``length`` chars at a time, incrementing
    ``offset_chars`` by ``length_returned`` each round.

    Returns ``{ok, content, offset_chars, end_offset, length_returned,
    total_chars, total_bytes, more, encoding, hint}``. ``content`` is
    NOT line-numbered; it's a raw character window. Use this together
    with ``grep_text(mode='context')`` and ``extract_strings`` to triage
    bundles without paying the full-file cost.
    """
    from argusai.core.infra import text_io

    return text_io.read_text_slice(
        path=str(path or ""),
        offset_chars=offset_chars,
        length=text_io.DEFAULT_SLICE_LENGTH if length is None else length,
        encoding=(encoding or None),
        base=None,
    )


def extract_strings(
    api: Any,
    path: str = "",
    min_len: Any = None,
    max_results: Any = None,
    kind: str = "any",
    min_entropy: Any = None,
    encoding: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Pull string literals / URLs / data-URIs / JWTs / base64 / hex
    constants out of a file, ``strings(1)``-style. Designed for huge
    minified JS or obfuscated bundles where the model needs to see the
    *data* without reading the surrounding code.

    Common patterns:

    - ``kind='url'`` — list every ``http(s)://`` endpoint the bundle
      talks to. Great for "what API does this site call?"
    - ``kind='base64'`` / ``kind='hex'`` — find candidate keys / IVs /
      hashes. Each result includes ``entropy`` so you can sort.
    - ``kind='jwt'`` — recover hardcoded JWTs from the bundle.
    - ``kind='data_uri'`` — find inlined images / fonts / WASM blobs.
    - ``kind='high_entropy'`` — string literals with Shannon entropy
      >= ``min_entropy`` (default 4.5). Use to find "the secret"
      without knowing its exact format.
    - ``kind='any'`` / ``kind='literal'`` — every string literal of
      length >= ``min_len`` (default 16). Use when you want a triage
      dump.

    Each item has ``offset`` / ``end`` so you can feed that range to
    ``read_text_slice`` for the surrounding code.
    """
    from argusai.core.infra import text_io

    return text_io.extract_strings(
        path=str(path or ""),
        min_len=text_io.DEFAULT_STRING_MIN_LEN if min_len is None else min_len,
        max_results=text_io.DEFAULT_STRING_RESULTS if max_results is None else max_results,
        kind=str(kind or "any"),
        min_entropy=min_entropy,
        encoding=(encoding or None),
        base=None,
    )


def grep_text_dir(
    api: Any,
    pattern: str = "",
    root: str = "",
    glob: str = "",
    mode: str = "content",
    max_matches: Any = None,
    max_files: Any = None,
    line_trunc: Any = None,
    case_insensitive: Any = False,
    regex: Any = True,
    exclude_dirs: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Recursive grep over a directory.

    Use this when you don't know which file to look in. Pass a ``glob``
    like ``"*.js"`` or ``"**/*.py"`` to narrow the scan. Returns
    ``path:line:text`` records (mode=content), file list
    (mode=files_with_matches), or counts.

    Common workflow:
      1) ``grep_text_dir(pattern='secretKey', root='./jobs/<id>/artifacts',
         mode='files_with_matches')`` → list of files
      2) ``grep_text(pattern='secretKey', path='<one of those files>')``
         → narrow lines
      3) ``read_text_file(path='<that file>', offset=<line>, limit=40)``
         → see context
    """
    from argusai.core.infra import text_io

    return text_io.grep_text_dir(
        pattern=str(pattern or ""),
        root=str(root or ""),
        glob=glob,
        mode=mode,
        max_matches=text_io.DEFAULT_GREP_MATCHES if max_matches is None else max_matches,
        max_files=text_io.MAX_FILES_SCANNED if max_files is None else max_files,
        line_trunc=text_io.MAX_GREP_LINE_LEN if line_trunc is None else line_trunc,
        case_insensitive=case_insensitive,
        regex=regex,
        exclude_dirs=exclude_dirs,
        base=None,
    )


# ----------------------------------------------------------------------
# Codebase audit — scoped source interaction for white-box testing.
#
# These tools deliberately avoid language-specific conclusions. They only
# provide a bounded source root, a map, search/read primitives, candidate
# entrypoint signals, and evidence capture so the model can reason over code
# without roaming the user's filesystem or dumping whole repositories.
# ----------------------------------------------------------------------

_CODEBASE_DEFAULT_EXCLUDES = [
    # Keep .cursor readable/writable: agent rules/skills live under .cursor/rules.
    ".git", ".hg", ".svn", ".idea", ".vscode",
    "node_modules", "bower_components", ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "dist", "build", "out", "target", ".next", ".nuxt", ".cache",
    "coverage", "vendor", "third_party",
]
_CODEBASE_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip",
    ".gz", ".tar", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".class", ".jar", ".war", ".pyc", ".pyd", ".wasm", ".woff", ".woff2",
    ".ttf", ".eot", ".mp4", ".mov", ".avi", ".mp3", ".wav",
}
_CODEBASE_CONFIG_NAMES = {
    "package.json", "pyproject.toml", "requirements.txt", "poetry.lock",
    "pipfile", "go.mod", "cargo.toml", "pom.xml", "build.gradle",
    "settings.py", "config.py", ".env", ".env.example", "dockerfile",
    "docker-compose.yml", "compose.yml", "nginx.conf", "apache.conf",
}
_CODEBASE_ENTRY_PATTERNS = [
    ("route", r"\b(route|router|endpoint|controller|handler|api)\b"),
    ("http_route", r"(@(?:Get|Post|Put|Delete|Patch)Mapping|@(app|router)\.(get|post|put|delete|patch)|\b(methods|path)\s*=)"),
    ("authz", r"\b(auth|authorize|permission|role|policy|guard|middleware|preauthorize|hasrole|hasauthority)\b"),
    ("database", r"\b(select|insert|update|delete|query|execute|rawsql|sequelize|prisma|typeorm|mongoose|jdbc)\b"),
    ("file_io", r"\b(open|readfile|writefile|read_file|write_file|filepath|upload|download|sendfile)\b"),
    ("command", r"\b(exec|spawn|popen|system|subprocess|shell_exec|processbuilder|runtime\.getruntime)\b"),
    ("network", r"\b(fetch|axios|requests\.|httpx\.|urllib|resttemplate|webclient|grpc|websocket)\b"),
    ("crypto", r"\b(secret|token|jwt|sign|signature|hmac|sha256|md5|aes|rsa|encrypt|decrypt)\b"),
    ("business", r"\b(order|payment|pay|coupon|tenant|admin|approval|invoice|refund|wallet|balance|export|import)\b"),
]


def _codebase_state(api: Any, session_id: str) -> dict[str, Any]:
    states = getattr(api, "_assistant_codebase_scopes", None)
    if not isinstance(states, dict):
        states = {}
        setattr(api, "_assistant_codebase_scopes", states)
    sid = session_id or "default"
    state = states.get(sid)
    if not isinstance(state, dict):
        state = {}
        states[sid] = state
    return state


def _codebase_session_id(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("_session_id") or "default")


def _split_excludes(value: Any) -> list[str]:
    import re

    if value is None:
        return list(_CODEBASE_DEFAULT_EXCLUDES)
    if isinstance(value, list):
        extra = [str(x).strip().strip("/\\") for x in value if str(x).strip()]
    else:
        extra = [s.strip().strip("/\\") for s in re.split(r"[,;\n]+", str(value or "")) if s.strip()]
    merged = list(_CODEBASE_DEFAULT_EXCLUDES)
    for item in extra:
        if item and item not in merged:
            merged.append(item)
    return merged


def _resolve_codebase_root(path: str) -> Any:
    from pathlib import Path

    root = Path(str(path or "").strip()).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"codebase root does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"codebase root is not a directory: {root}")
    return root


def _ensure_workspace_codebase_scope(api: Any, kwargs: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Bind codebase scope to the session's local project directory when unset.

    Creating a conversation with workspace_mode=project stores the folder on
    the session, but codebase_* tools previously required an extra
    ``codebase_scope_set`` call. Fill that gap once so the Agent actually
    works in the chosen directory. An explicit ``codebase_scope_set`` still
    wins and is not overwritten.
    """
    payload = kwargs if isinstance(kwargs, dict) else {}
    project = _session_project_dir(api, payload)
    if project is None:
        return None
    state = _codebase_state(api, _codebase_session_id(payload))
    if str(state.get("root") or "").strip():
        return state
    state.update({
        "root": str(project),
        "audit_goal": str(state.get("audit_goal") or ""),
        "case_id": str(state.get("case_id") or ""),
        "excludes": list(state.get("excludes") or _CODEBASE_DEFAULT_EXCLUDES),
        "source": "session_workspace",
    })
    return state


def _codebase_scope(api: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    _ensure_workspace_codebase_scope(api, kwargs)
    state = _codebase_state(api, _codebase_session_id(kwargs))
    root = state.get("root")
    if not root:
        raise ValueError("codebase scope is not set; call codebase_scope_set first")
    return state


def _resolve_codebase_path(api: Any, kwargs: dict[str, Any], path: str = "") -> Any:
    from pathlib import Path

    state = _codebase_scope(api, kwargs)
    root = Path(state["root"]).resolve()
    raw = str(path or ".").strip() or "."
    candidate = Path(raw).expanduser()
    resolved = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as e:
        raise ValueError(f"path escapes codebase scope: {path}") from e
    return resolved


def _codebase_rel(root: Any, path: Any) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except Exception:
        return str(path)


def _codebase_is_ignored(path: Any, root: Any, excludes: list[str]) -> bool:
    import fnmatch

    rel = _codebase_rel(root, path)
    parts = rel.split("/")
    for ex in excludes:
        if not ex:
            continue
        ex_norm = ex.replace("\\", "/").strip("/")
        if ex_norm in parts:
            return True
        if fnmatch.fnmatch(rel, ex_norm) or fnmatch.fnmatch(rel, ex_norm + "/*"):
            return True
    return False


def _codebase_file_kind(path: Any) -> str:
    name = path.name.lower()
    suffix = path.suffix.lower()
    if suffix in _CODEBASE_BINARY_EXTS:
        return "binary"
    if name in _CODEBASE_CONFIG_NAMES:
        return "config"
    if suffix in {".md", ".rst", ".txt"}:
        return "doc"
    if suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".xml"}:
        return "config"
    return "source"


def codebase_scope_set(
    api: Any,
    root: str = "",
    audit_goal: str = "",
    case_id: str = "",
    excludes: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Set the current session's source-code audit root and scope limits."""
    resolved = _resolve_codebase_root(root)
    exclude_list = _split_excludes(excludes)
    state = _codebase_state(api, _codebase_session_id(kwargs))
    state.clear()
    state.update({
        "root": str(resolved),
        "audit_goal": str(audit_goal or ""),
        "case_id": str(case_id or ""),
        "excludes": exclude_list,
    })
    return {
        "ok": True,
        "root": str(resolved),
        "audit_goal": state["audit_goal"],
        "case_id": state["case_id"],
        "excludes": exclude_list,
        "next": "Call codebase_tree or codebase_entrypoints to build an initial source map.",
    }


def codebase_scope_get(api: Any, **kwargs: Any) -> dict[str, Any]:
    """Return the current session's source-code audit scope."""
    _ensure_workspace_codebase_scope(api, kwargs)
    state = _codebase_state(api, _codebase_session_id(kwargs))
    if not state.get("root"):
        return {"ok": False, "error": "codebase scope is not set"}
    return {"ok": True, **state}


def codebase_tree(
    api: Any,
    path: str = ".",
    max_depth: int = 3,
    offset: int = 0,
    limit: int = 200,
    include_files: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """List a scoped repository tree with pagination and lightweight stats."""
    import os
    from collections import Counter
    from pathlib import Path

    state = _codebase_scope(api, kwargs)
    root = Path(state["root"]).resolve()
    try:
        start = _resolve_codebase_path(api, kwargs, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not start.exists() or not start.is_dir():
        return {"ok": False, "error": f"not a directory inside scope: {path}"}
    excludes = list(state.get("excludes") or _CODEBASE_DEFAULT_EXCLUDES)
    depth_cap = _safe_int(max_depth, 3, 0, 12)
    off = _safe_int(offset, 0, 0, 1_000_000)
    cap = _safe_int(limit, 200, 1, 1000)
    entries: list[dict[str, Any]] = []
    ext_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    skipped_dirs: list[str] = []
    total_files = 0
    total_dirs = 0

    for dirpath, dirnames, filenames in os.walk(start):
        current = Path(dirpath)
        rel_current = _codebase_rel(root, current)
        depth = 0 if rel_current == "." else len(rel_current.split("/"))
        if depth > depth_cap:
            dirnames[:] = []
            continue
        kept_dirs: list[str] = []
        for d in sorted(dirnames):
            p = current / d
            if _codebase_is_ignored(p, root, excludes):
                if len(skipped_dirs) < 50:
                    skipped_dirs.append(_codebase_rel(root, p))
                continue
            kept_dirs.append(d)
            total_dirs += 1
            entries.append({"type": "dir", "path": _codebase_rel(root, p), "depth": depth + 1})
        dirnames[:] = kept_dirs
        if include_files:
            for fn in sorted(filenames):
                p = current / fn
                if _codebase_is_ignored(p, root, excludes):
                    continue
                total_files += 1
                suffix = p.suffix.lower() or "<none>"
                kind = _codebase_file_kind(p)
                ext_counts[suffix] += 1
                kind_counts[kind] += 1
                try:
                    size = p.stat().st_size
                except Exception:
                    size = 0
                entries.append({
                    "type": "file",
                    "path": _codebase_rel(root, p),
                    "depth": depth + 1,
                    "size_bytes": size,
                    "kind": kind,
                })
    page = entries[off: off + cap]
    return {
        "ok": True,
        "root": str(root),
        "path": _codebase_rel(root, start),
        "entries": page,
        "entry_count_returned": len(page),
        "entry_count_total": len(entries),
        "offset": off,
        "more": off + cap < len(entries),
        "next_offset": off + cap if off + cap < len(entries) else None,
        "stats": {
            "files_seen": total_files,
            "dirs_seen": total_dirs,
            "extensions": dict(ext_counts.most_common(30)),
            "kinds": dict(kind_counts),
            "skipped_dirs_sample": skipped_dirs,
        },
    }


def codebase_search(
    api: Any,
    pattern: str = "",
    path: str = ".",
    glob: str = "",
    mode: str = "content",
    max_matches: Any = None,
    max_files: Any = None,
    case_insensitive: Any = True,
    regex: Any = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Search inside the scoped codebase; use before reading files."""
    from pathlib import Path
    from argusai.core.infra import text_io

    state = _codebase_scope(api, kwargs)
    root = Path(state["root"]).resolve()
    try:
        start = _resolve_codebase_path(api, kwargs, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if not start.exists():
        return {"ok": False, "error": f"path not found inside scope: {path}"}
    if start.is_file():
        result = text_io.grep_text_file(
            pattern=str(pattern or ""),
            path=str(start),
            mode=mode,
            max_matches=text_io.DEFAULT_GREP_MATCHES if max_matches is None else max_matches,
            line_trunc=text_io.MAX_GREP_LINE_LEN,
            context_chars=text_io.DEFAULT_GREP_CONTEXT,
            case_insensitive=case_insensitive,
            regex=regex,
            encoding=None,
            base=str(root),
        )
        if result.get("ok"):
            result["relative_path"] = _codebase_rel(root, start)
        return result
    result = text_io.grep_text_dir(
        pattern=str(pattern or ""),
        root=str(start),
        glob=glob,
        mode=mode,
        max_matches=text_io.DEFAULT_GREP_MATCHES if max_matches is None else max_matches,
        max_files=text_io.MAX_FILES_SCANNED if max_files is None else max_files,
        line_trunc=text_io.MAX_GREP_LINE_LEN,
        case_insensitive=case_insensitive,
        regex=regex,
        exclude_dirs=list(state.get("excludes") or _CODEBASE_DEFAULT_EXCLUDES),
        base=str(root),
    )
    if result.get("ok"):
        for item in result.get("matches") or []:
            p = item.get("path")
            if p:
                item["relative_path"] = _codebase_rel(root, Path(p))
        if isinstance(result.get("files"), list):
            result["relative_files"] = [_codebase_rel(root, Path(p)) for p in result["files"]]
        result["root"] = str(root)
        result["searched_path"] = _codebase_rel(root, start)
    return result


def codebase_read(
    api: Any,
    path: str = "",
    offset: Any = 1,
    limit: Any = None,
    line_trunc: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Read a scoped source file window with line numbers.

    Raster images inside the codebase are attached for vision instead of
    being decoded as text.
    """
    from pathlib import Path
    from argusai.core.assistant.image_io import image_read_tool_result, is_raster_image_path
    from argusai.core.infra import text_io

    state = _codebase_scope(api, kwargs)
    root = Path(state["root"]).resolve()
    try:
        target = _resolve_codebase_path(api, kwargs, path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if is_raster_image_path(target):
        result = image_read_tool_result(target)
        if result.get("ok"):
            result["relative_path"] = _codebase_rel(root, target)
        return result
    result = text_io.read_text_file(
        path=str(target),
        offset=offset,
        limit=text_io.DEFAULT_LIMIT_LINES if limit is None else limit,
        encoding=None,
        line_trunc=text_io.DEFAULT_LINE_TRUNC if line_trunc is None else line_trunc,
        base=str(root),
    )
    if result.get("ok"):
        result["relative_path"] = _codebase_rel(root, target)
    return result


def codebase_entrypoints(
    api: Any,
    path: str = ".",
    max_per_kind: int = 25,
    max_files: int = 3000,
    **kwargs: Any,
) -> dict[str, Any]:
    """Extract language-agnostic entrypoint and risk candidate signals."""
    from collections import defaultdict

    cap = _safe_int(max_per_kind, 25, 1, 100)
    file_cap = _safe_int(max_files, 3000, 10, 5000)
    signals: dict[str, list[dict[str, Any]]] = defaultdict(list)
    searched = 0
    for kind, pattern in _CODEBASE_ENTRY_PATTERNS:
        remaining = cap - len(signals[kind])
        if remaining <= 0:
            continue
        result = codebase_search(
            api,
            pattern=pattern,
            path=path,
            mode="content",
            max_matches=remaining,
            max_files=file_cap,
            case_insensitive=True,
            regex=True,
            **kwargs,
        )
        searched += int(result.get("files_scanned") or 0)
        for item in result.get("matches") or []:
            signals[kind].append({
                "path": item.get("relative_path") or item.get("path"),
                "line": item.get("line"),
                "text": item.get("text"),
            })
    config = codebase_tree(api, path=path, max_depth=2, limit=500, include_files=True, **kwargs)
    config_files = [
        {"path": e.get("path"), "size_bytes": e.get("size_bytes")}
        for e in config.get("entries", [])
        if e.get("type") == "file" and e.get("kind") == "config"
    ][:cap]
    return {
        "ok": True,
        "path": path,
        "signals": dict(signals),
        "config_files": config_files,
        "files_scanned_hint": searched,
        "note": "These are candidate signals for model review, not vulnerability conclusions.",
    }


def codebase_evidence_add(
    api: Any,
    case_id: str = "",
    path: str = "",
    start_line: int = 1,
    end_line: int = 80,
    title: str = "",
    summary: str = "",
    confidence: str = "suspected",
    kind: str = "source",
    **kwargs: Any,
) -> dict[str, Any]:
    """Attach a scoped source snippet and evidence record to a case."""
    import hashlib
    import json
    from pathlib import Path

    state = _codebase_scope(api, kwargs)
    root = Path(state["root"]).resolve()
    cid = str(case_id or state.get("case_id") or "").strip()
    if not cid:
        return {"ok": False, "error": "case_id is required (or set one in codebase_scope_set)"}
    rel_path = str(path or "").strip()
    if not rel_path:
        return {"ok": False, "error": "path is required"}
    try:
        _resolve_codebase_path(api, kwargs, rel_path)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    start = _safe_int(start_line, 1, 1, 100_000_000)
    end = _safe_int(end_line, start, start, 100_000_000)
    limit = max(1, min(400, end - start + 1))
    read = codebase_read(api, path=rel_path, offset=start, limit=limit, **kwargs)
    if not read.get("ok"):
        return read
    content = str(read.get("content") or "")
    rel = read.get("relative_path") or _codebase_rel(root, _resolve_codebase_path(api, kwargs, rel_path))
    snippet_hash = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
    artifact = artifact_add(
        api,
        case_id=cid,
        kind="source",
        title=title or f"Source evidence: {rel}:{start}-{read.get('end_line')}",
        content=content,
        filename=f"{rel.replace('/', '_')}.{start}-{read.get('end_line')}.txt",
        domain="repository",
        media_type="text/plain",
        metadata_json=json.dumps({
            "source_root": str(root),
            "path": rel,
            "start_line": start,
            "end_line": read.get("end_line"),
            "sha256": snippet_hash,
            "tool": "codebase_evidence_add",
        }, ensure_ascii=False),
    )
    if not artifact.get("ok"):
        return artifact
    evidence = evidence_add(
        api,
        case_id=cid,
        kind=kind or "source",
        title=title or f"Source evidence: {rel}",
        confidence=confidence,
        artifact_refs=[artifact.get("artifact_ref")],
        summary=summary or f"Source snippet from {rel}:{start}-{read.get('end_line')}",
        metadata_json=json.dumps({
            "path": rel,
            "start_line": start,
            "end_line": read.get("end_line"),
            "sha256": snippet_hash,
        }, ensure_ascii=False),
    )
    return {
        "ok": bool(evidence.get("ok")),
        "case_id": cid,
        "path": rel,
        "start_line": start,
        "end_line": read.get("end_line"),
        "sha256": snippet_hash,
        "artifact_ref": artifact.get("artifact_ref"),
        "evidence_ref": evidence.get("evidence_ref"),
        "artifact": artifact.get("artifact"),
        "evidence": evidence.get("evidence"),
    }


# ----------------------------------------------------------------------
# Codebase workspace — Claude Code / Cursor-style file I/O + run inside
# codebase_scope_set root (not the session sandbox).
# ----------------------------------------------------------------------

_CODEBASE_TEXT_EXTS = {
    ".py", ".pyi", ".pyw",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".fs", ".vb",
    ".rb", ".php", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hxx",
    ".m", ".mm", ".swift", ".scala", ".clj", ".ex", ".exs", ".erl",
    ".hs", ".lua", ".r", ".jl", ".pl", ".pm",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
    ".sql", ".graphql", ".gql", ".proto", ".thrift",
    ".toml", ".ini", ".cfg", ".conf", ".xml", ".xsl", ".xsd",
    ".css", ".scss", ".sass", ".less",
    ".html", ".htm", ".md", ".mdx", ".rst", ".txt",
    ".json", ".jsonc", ".yaml", ".yml",
    ".gradle", ".cmake", ".mk", ".wat",
}
_CODEBASE_TEXT_NAMES = {
    "dockerfile", "makefile", "cmakelists.txt", "gemfile", "rakefile",
    "procfile", "vagrantfile", "jenkinsfile", "brewfile",
    "license", "licence", "authors", "contributors", "changelog",
    "agents.md", "claude.md", "gemini.md", "contributing.md",
    "readme", "readme.md",
    ".gitignore", ".gitattributes", ".editorconfig", ".nvmrc",
    ".python-version", ".node-version", ".npmrc", ".prettierrc",
    ".cursorrules", ".dockerignore", ".eslintrc", ".babelrc",
}
_CODEBASE_SECRET_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development",
    ".env.staging", ".env.test",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
}
_CODEBASE_SECRET_EXTS = {".pem", ".p12", ".pfx", ".key", ".keystore", ".jks"}
_CODEBASE_OPENABLE_EXTS = {
    ".txt", ".md", ".markdown", ".mdx", ".rst", ".json", ".jsonc",
    ".yaml", ".yml", ".toml", ".csv", ".tsv",
    ".html", ".htm", ".css", ".js", ".ts", ".tsx", ".jsx", ".vue",
    ".xml", ".log", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg",
}


def _codebase_is_secret_path(path: Any) -> bool:
    name = str(getattr(path, "name", "") or "").lower()
    suffix = str(getattr(path, "suffix", "") or "").lower()
    if name in _CODEBASE_SECRET_NAMES:
        return True
    if name.startswith(".env.") or name.startswith(".env_"):
        return True
    if suffix in _CODEBASE_SECRET_EXTS:
        return True
    return False


def _codebase_allows_text_path(path: Any) -> bool:
    name = str(getattr(path, "name", "") or "")
    suffix = str(getattr(path, "suffix", "") or "").lower()
    if suffix in _CODEBASE_TEXT_EXTS:
        return True
    if name.lower() in _CODEBASE_TEXT_NAMES:
        return True
    # Dotfile configs without extension (e.g. .gitignore already covered).
    if name.startswith(".") and "." not in name[1:] and name.lower() in _CODEBASE_TEXT_NAMES:
        return True
    return False


def _codebase_guard_mutable_path(api: Any, kwargs: dict[str, Any], path: str, *, for_delete: bool = False) -> dict[str, Any]:
    """Resolve a mutable codebase path or return ``{ok: False, error}``."""
    from pathlib import Path

    raw = str(path or "").strip()
    if not raw:
        return {"ok": False, "error": "path is required"}
    try:
        state = _codebase_scope(api, kwargs)
        root = Path(state["root"]).resolve()
        target = _resolve_codebase_path(api, kwargs, raw)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    excludes = list(state.get("excludes") or _CODEBASE_DEFAULT_EXCLUDES)
    if _codebase_is_ignored(target, root, excludes):
        return {
            "ok": False,
            "error": f"path is excluded from codebase scope: {_codebase_rel(root, target)}",
            "error_code": "codebase_path_excluded",
        }
    if _codebase_is_secret_path(target):
        return {
            "ok": False,
            "error": f"refusing to {'delete' if for_delete else 'write'} secret-like path: {target.name}",
            "error_code": "codebase_secret_blocked",
        }
    suffix = target.suffix.lower()
    if suffix in _CODEBASE_BINARY_EXTS:
        return {
            "ok": False,
            "error": f"binary extension not writable via codebase tools: {suffix}",
            "error_code": "codebase_binary_blocked",
        }
    if not for_delete and not _codebase_allows_text_path(target):
        return {
            "ok": False,
            "error": (
                f"extension not in codebase text allowlist: {suffix or '(none)'}. "
                "Use a known source/config extension or ask to extend the allowlist."
            ),
            "error_code": "codebase_ext_blocked",
        }
    return {"ok": True, "root": root, "target": target, "relative_path": _codebase_rel(root, target), "state": state}


def codebase_write_file(api: Any, path: str = "", content: str = "", **kwargs: Any) -> dict[str, Any]:
    """Create or overwrite a text file inside the current codebase scope.

    Requires ``codebase_scope_set`` first. Prefer ``codebase_patch_file`` for
    small edits. Paths must stay inside the scoped root and outside excludes.
    """
    from argusai.core.infra import text_io

    guarded = _codebase_guard_mutable_path(api, kwargs, path)
    if not guarded.get("ok"):
        return guarded
    result = text_io.write_text_file_resolved(guarded["target"], content)
    if not result.get("ok"):
        return result
    return {
        **result,
        "relative_path": guarded["relative_path"],
        "root": str(guarded["root"]),
        "workspace": "codebase",
        "hint": (
            "Wrote into the user repository (codebase scope). "
            "Prefer codebase_patch_file for small edits; verify with codebase_run_shell_*."
        ),
    }


def codebase_patch_file(
    api: Any,
    path: str = "",
    old_string: str = "",
    new_string: str = "",
    replace_all: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Exact substring patch inside a scoped repository file.

    Copy ``old_string`` from ``codebase_read`` content (not invented). Requires
    ``codebase_scope_set``.
    """
    from argusai.core.infra import text_io

    guarded = _codebase_guard_mutable_path(api, kwargs, path)
    if not guarded.get("ok"):
        return guarded
    target = guarded["target"]
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "file not found", "path": str(target), "relative_path": guarded["relative_path"]}
    result = text_io.patch_text_file_resolved(
        target,
        old_string,
        new_string,
        replace_all=bool(replace_all),
    )
    if not result.get("ok"):
        if result.get("error_code") == "patch_old_string_not_found":
            result["suggested_fix"] = (
                "Re-read with codebase_read and copy the exact substring, then retry codebase_patch_file."
            )
        result["relative_path"] = guarded["relative_path"]
        return result
    return {
        **result,
        "relative_path": guarded["relative_path"],
        "root": str(guarded["root"]),
        "workspace": "codebase",
        "hint": "Patched user-repository file inside codebase scope.",
    }


def codebase_delete_file(api: Any, path: str = "", **kwargs: Any) -> dict[str, Any]:
    """Delete a text file inside the current codebase scope."""
    guarded = _codebase_guard_mutable_path(api, kwargs, path, for_delete=True)
    if not guarded.get("ok"):
        return guarded
    target = guarded["target"]
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "file not found", "path": str(target), "relative_path": guarded["relative_path"]}
    try:
        size = target.stat().st_size
        target.unlink()
    except Exception as exc:
        return {"ok": False, "error": f"delete failed: {exc}", "path": str(target)}
    return {
        "ok": True,
        "path": str(target),
        "relative_path": guarded["relative_path"],
        "root": str(guarded["root"]),
        "size": size,
        "deleted": True,
        "workspace": "codebase",
    }


def codebase_stat_file(api: Any, path: str = "", **kwargs: Any) -> dict[str, Any]:
    """Stat a file inside the current codebase scope."""
    from pathlib import Path

    try:
        state = _codebase_scope(api, kwargs)
        root = Path(state["root"]).resolve()
        target = _resolve_codebase_path(api, kwargs, path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "file not found", "path": str(target)}
    size = target.stat().st_size
    line_count: int | None = None
    if size <= 20_000_000 and target.suffix.lower() not in _CODEBASE_BINARY_EXTS:
        try:
            with target.open("rb") as fh:
                line_count = sum(1 for _ in fh)
        except Exception:
            line_count = None
    rel = _codebase_rel(root, target)
    return {
        "ok": True,
        "path": str(target),
        "relative_path": rel,
        "root": str(root),
        "size": size,
        "line_count": line_count,
        "suffix": target.suffix.lower(),
        "large_file": size > 256 * 1024,
        "workspace": "codebase",
    }


def codebase_grep_file(
    api: Any,
    path: str = "",
    pattern: str = "",
    mode: str = "content",
    max_matches: Any = None,
    context_chars: Any = None,
    case_insensitive: Any = False,
    regex: Any = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Grep inside one scoped repository file (search before read)."""
    from pathlib import Path
    from argusai.core.infra import text_io

    try:
        state = _codebase_scope(api, kwargs)
        root = Path(state["root"]).resolve()
        target = _resolve_codebase_path(api, kwargs, path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "file not found", "path": str(target)}
    rel = _codebase_rel(root, target)
    result = text_io.grep_text_file(
        pattern=str(pattern or ""),
        path=rel,
        mode=mode,
        max_matches=text_io.DEFAULT_GREP_MATCHES if max_matches is None else max_matches,
        context_chars=text_io.DEFAULT_GREP_CONTEXT if context_chars is None else context_chars,
        case_insensitive=case_insensitive,
        regex=regex,
        base=str(root),
    )
    if result.get("ok"):
        result["relative_path"] = rel
        result["workspace"] = "codebase"
        if str(mode or "content").lower() == "content" and result.get("matches"):
            first_line = result["matches"][0].get("line")
            result["hint"] = (
                f"First hit at line {first_line}. "
                f"Call codebase_read(path={rel!r}, offset={first_line}, limit=40)."
            )
    return result


def codebase_list_files(
    api: Any,
    path: str = ".",
    max_depth: int = 2,
    limit: int = 200,
    **kwargs: Any,
) -> dict[str, Any]:
    """List files/dirs under a path inside the current codebase scope."""
    tree = codebase_tree(
        api,
        path=path,
        max_depth=_safe_int(max_depth, 2, 0, 12),
        limit=_safe_int(limit, 200, 1, 1000),
        include_files=True,
        **kwargs,
    )
    if not tree.get("ok"):
        return tree
    return {
        "ok": True,
        "path": tree.get("path") or path,
        "entries": tree.get("entries") or [],
        "workspace": "codebase",
        "root": tree.get("root"),
    }


def codebase_open_file(api: Any, path: str = "", **kwargs: Any) -> dict[str, Any]:
    """Open a scoped repository file in the OS default application."""
    import os
    import platform
    import subprocess
    from pathlib import Path

    try:
        state = _codebase_scope(api, kwargs)
        root = Path(state["root"]).resolve()
        target = _resolve_codebase_path(api, kwargs, path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not target.exists() or not target.is_file():
        return {"ok": False, "error": "file not found", "path": str(target)}
    if target.suffix.lower() not in _CODEBASE_OPENABLE_EXTS:
        return {
            "ok": False,
            "error": f"extension not safe to open via OS handler: {target.suffix or '(none)'}",
            "error_code": "codebase_open_ext_blocked",
        }
    system = platform.system().lower()
    try:
        if system.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif system == "darwin":
            subprocess.Popen(["open", str(target)], close_fds=True)
        else:
            subprocess.Popen(["xdg-open", str(target)], close_fds=True)
    except Exception as exc:
        return {"ok": False, "error": f"OS shell open failed: {exc}", "path": str(target)}
    return {
        "ok": True,
        "path": str(target),
        "relative_path": _codebase_rel(root, target),
        "workspace": "codebase",
        "platform": system,
    }


def codebase_reveal_in_folder(api: Any, path: str = "", **kwargs: Any) -> dict[str, Any]:
    """Reveal a scoped path (or the codebase root) in the OS file manager."""
    from pathlib import Path
    from argusai.core.infra.explorer import reveal_path

    try:
        state = _codebase_scope(api, kwargs)
        root = Path(state["root"]).resolve()
        if not str(path or "").strip():
            target = root
        else:
            target = _resolve_codebase_path(api, kwargs, path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not target.exists():
        return {"ok": False, "error": "path not found", "path": str(target)}
    result = reveal_path(target)
    result["workspace"] = "codebase"
    result["relative_path"] = _codebase_rel(root, target) if target != root else "."
    return result


def _codebase_python_cmd(root: Any, python_env: str = "") -> list[str]:
    import platform
    from pathlib import Path

    root_p = Path(root)
    if platform.system().lower().startswith("win"):
        venv_py = root_p / ".venv" / "Scripts" / "python.exe"
    else:
        venv_py = root_p / ".venv" / "bin" / "python"
    if not str(python_env or "").strip() and venv_py.is_file():
        return [str(venv_py)]
    from argusai.core.workspace.session_sandbox import _direct_python_command

    return _direct_python_command(str(python_env or "").strip())


def _codebase_run_audit_root() -> Any:
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(prefix="argusai-codebase-run-"))


async def codebase_run_python_script(
    api: Any,
    path: str = "",
    python_env: str = "",
    args: list[str] | str | None = None,
    timeout_seconds: int = 30,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a ``.py`` file that lives inside the codebase scope (cwd = scope root)."""
    from pathlib import Path
    from argusai.core.workspace.session_sandbox import _parse_args, _run_exec, _timeout

    try:
        state = _codebase_scope(api, kwargs)
        root = Path(state["root"]).resolve()
        target = _resolve_codebase_path(api, kwargs, path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not target.is_file() or target.suffix.lower() != ".py":
        return {"ok": False, "error": "python script (.py) not found inside codebase scope", "path": str(target)}
    excludes = list(state.get("excludes") or _CODEBASE_DEFAULT_EXCLUDES)
    if _codebase_is_ignored(target, root, excludes):
        return {"ok": False, "error": "path is excluded from codebase scope", "error_code": "codebase_path_excluded"}
    cmd = _codebase_python_cmd(root, python_env)
    if not cmd:
        return {
            "ok": False,
            "error": "no Python interpreter available for codebase run",
            "error_code": "codebase_python_missing",
            "hint": "Install Python or create scope/.venv, then retry.",
        }
    argv_args = _parse_args(args)
    audit = _codebase_run_audit_root()
    result = await _run_exec(
        [*cmd, str(target), *argv_args],
        root,
        _timeout(timeout_seconds),
        audit,
        "codebase_run_python_script",
        {"path": str(target), "relative_path": _codebase_rel(root, target), "python": cmd},
    )
    result["workspace"] = "codebase"
    result["cwd"] = str(root)
    result["relative_path"] = _codebase_rel(root, target)
    return result


async def codebase_run_python_snippet(
    api: Any,
    code: str = "",
    python_env: str = "",
    args: list[str] | str | None = None,
    timeout_seconds: int = 30,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run inline Python with cwd = codebase scope root (ephemeral script file)."""
    from pathlib import Path

    if not str(code or "").strip():
        return {"ok": False, "error": "code is required", "error_code": "invalid_arguments"}
    try:
        state = _codebase_scope(api, kwargs)
        root = Path(state["root"]).resolve()
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    audit = _codebase_run_audit_root()
    script = audit / "snippet.py"
    script.write_text(str(code), encoding="utf-8", newline="\n")
    # Resolve as if under root for relative_path reporting only; execution uses absolute script path.
    from argusai.core.workspace.session_sandbox import _parse_args, _run_exec, _timeout

    cmd = _codebase_python_cmd(root, python_env)
    if not cmd:
        return {
            "ok": False,
            "error": "no Python interpreter available for codebase run",
            "error_code": "codebase_python_missing",
        }
    argv_args = _parse_args(args)
    result = await _run_exec(
        [*cmd, str(script), *argv_args],
        root,
        _timeout(timeout_seconds),
        audit,
        "codebase_run_python_snippet",
        {"snippet_path": str(script), "python": cmd},
    )
    result["workspace"] = "codebase"
    result["cwd"] = str(root)
    result["snippet_path"] = str(script)
    return result


async def codebase_run_shell_script(
    api: Any,
    path: str = "",
    shell: str = "",
    args: list[str] | str | None = None,
    timeout_seconds: int = 30,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run a shell script that lives inside the codebase scope (cwd = scope root)."""
    from pathlib import Path
    from argusai.core.workspace import session_sandbox as sb

    try:
        state = _codebase_scope(api, kwargs)
        root = Path(state["root"]).resolve()
        target = _resolve_codebase_path(api, kwargs, path)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not target.is_file():
        return {"ok": False, "error": "script not found", "path": str(target)}
    if target.suffix.lower() not in {".ps1", ".bat", ".cmd", ".sh", ".bash"}:
        return {"ok": False, "error": "shell script must be .ps1/.bat/.cmd/.sh", "path": str(target)}
    excludes = list(state.get("excludes") or _CODEBASE_DEFAULT_EXCLUDES)
    if _codebase_is_ignored(target, root, excludes):
        return {"ok": False, "error": "path is excluded from codebase scope", "error_code": "codebase_path_excluded"}
    try:
        body = target.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"ok": False, "error": f"read failed: {exc}"}
    reason = sb._dangerous_shell_reason(body)
    if reason:
        return {"ok": False, "error": reason}
    shell_name = shell or sb._default_shell_for_ext(target.suffix)
    argv_args = sb._parse_args(args)
    # Build argv like run_shell_script but with cwd=root and no sandbox venv PATH.
    if sb._is_powershell_shell(shell_name):
        exe = "powershell" if shell_name in {"powershell", "windows-powershell"} else (shell_name or "pwsh")
        argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(target), *argv_args]
    elif shell_name == "cmd":
        argv = ["cmd", "/c", str(target), *argv_args]
    else:
        argv = [shell_name or "bash", str(target), *argv_args]
    audit = _codebase_run_audit_root()
    result = await sb._run_exec(
        argv,
        root,
        sb._timeout(timeout_seconds),
        audit,
        "codebase_run_shell_script",
        {"path": str(target), "relative_path": _codebase_rel(root, target), "shell": shell_name},
    )
    result["workspace"] = "codebase"
    result["cwd"] = str(root)
    result["relative_path"] = _codebase_rel(root, target)
    result["shell"] = shell_name
    return result


async def codebase_run_shell_snippet(
    api: Any,
    script: str = "",
    shell: str = "",
    timeout_seconds: int = 30,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run an inline shell command with cwd = codebase scope root (polyglot main path)."""
    from pathlib import Path
    from argusai.core.workspace import session_sandbox as sb

    if not str(script or "").strip():
        return {"ok": False, "error": "script is required", "error_code": "invalid_arguments"}
    try:
        state = _codebase_scope(api, kwargs)
        root = Path(state["root"]).resolve()
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    reason = sb._dangerous_shell_reason(script)
    if reason:
        return {"ok": False, "error": reason, "cwd": str(root)}
    default_shell = "auto"
    try:
        default_shell = str(getattr(api.settings_mgr.get(), "assistant_default_shell", "auto") or "auto")
    except Exception:
        pass
    shell_name, shell_switch_note, shell_error = sb._resolve_snippet_shell(
        shell,
        str(script or ""),
        default_shell=default_shell,
    )
    if shell_error:
        return {
            "ok": False,
            "error_code": "shell_mismatch",
            "error": shell_error,
            "cwd": str(root),
            "shell": shell_name,
            "retryable": True,
        }
    ext = ".ps1" if sb._is_powershell_shell(shell_name) else (".cmd" if shell_name == "cmd" else ".sh")
    audit = _codebase_run_audit_root()
    body = str(script or "")
    if sb._is_powershell_shell(shell_name):
        if "[Console]::OutputEncoding" not in body and "chcp 65001" not in body:
            body = sb._PS_UTF8_PROLOGUE + body
    elif shell_name == "cmd":
        if "chcp 65001" not in body:
            body = sb._CMD_UTF8_PROLOGUE + body
    elif shell_name in {"bash", "sh"}:
        body = sb._normalize_bash_newlines(body)
    script_path = audit / f"snippet{ext}"
    payload = body.encode("utf-8")
    if sb._is_powershell_shell(shell_name) or shell_name == "cmd":
        if not payload.startswith(b"\xef\xbb\xbf"):
            payload = b"\xef\xbb\xbf" + payload
    script_path.write_bytes(payload)

    if sb._is_powershell_shell(shell_name):
        exe = "powershell" if shell_name in {"powershell", "windows-powershell"} else (shell_name or "pwsh")
        argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
    elif shell_name == "cmd":
        argv = ["cmd", "/c", str(script_path)]
    else:
        argv = [shell_name or "bash", str(script_path)]
    result = await sb._run_exec(
        argv,
        root,
        sb._timeout(timeout_seconds),
        audit,
        "codebase_run_shell_snippet",
        {"snippet_path": str(script_path), "shell": shell_name},
    )
    result["workspace"] = "codebase"
    result["cwd"] = str(root)
    result["shell"] = shell_name
    result["snippet_path"] = str(script_path)
    if shell_switch_note:
        result["shell_auto_switch"] = shell_switch_note
    return result


# ----------------------------------------------------------------------
# Python-first reverse engineering — run JS without a browser, do
# crypto in Python, ship a one-click PoC.
#
# These exist to break the "I need to open the browser for the final
# PoC" anti-pattern. When you've deobfuscated a bundle and identified
# the entry function, prefer to:
#   1) Run that entry function in managed Node via run_js_function
#      (no DOM, no fetch, just pure JS — much faster than a browser).
#   2) Reproduce any verifiable crypto step in Python via crypto_reproduce
#      (golden-vector matching catches "wrong key encoding" mistakes
#      cheaply).
#   3) Package the result as a one-click Python bundle via
#      create_python_poc, which generates run.py + requirements.txt +
#      README.md on disk.
# ----------------------------------------------------------------------


def run_node_script(
    api: Any,
    source: str = "",
    path: str = "",
    mode: str = "script",
    function_name: str = "",
    args: Any = None,
    globals: Any = None,
    inner_timeout_ms: int = 5000,
    process_timeout_seconds: int = 30,
    env: str = "node",
    page_url: str = "",
    user_agent: str = "",
    profile_json: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Run JavaScript in the managed Node.js sandbox — NO browser.

    Use this for PURE-COMPUTATION JS: deobfuscated decoders, string-array
    decryption routines, signature builders, anything that doesn't touch
    DOM / fetch / localStorage. The classic case is "I just decrypted
    the 859 KB bundle and want to call its entry function from Python
    so I can build the final POST body without opening Chrome."

    Modes:

    - ``script``: DEFAULT. Run ``source`` verbatim as a complete JS
      program. Use this for nearly every multi-line snippet, including
      ``console.log(...)``, ``const`` / ``let`` / ``var`` declarations,
      function definitions, ``require`` / ``import``, semicolons, loops,
      try/catch, or any code that looks like statements instead of one
      expression. To return a value, set ``globalThis.__result = <value>``
      somewhere in the script.
    - ``eval``: ONLY for a single JavaScript expression whose value should
      be returned, e.g. ``1 + 2``, ``process.version``,
      ``someObject.field`` or ``decode('abc')``. Do NOT use ``eval`` for
      ``console.log(...)`` debugging snippets, statement blocks, or
      multiple lines; use ``script`` instead.
    - ``call``: run ``source`` (which must define a named function),
      then invoke ``function_name`` with ``args`` (a JSON list).
      Promises are awaited within ``inner_timeout_ms``.

    Environments:

    - ``env="node"`` (default): clean Node sandbox with console / Buffer
      / crypto / TextEncoder.
    - ``env="browser"``: also injects stub ``window`` / ``document`` /
      ``navigator`` / ``location`` / ``localStorage`` / ``performance``
      / ``fetch`` / ``XMLHttpRequest`` AND hides Node-only globals
      (``process``, ``Buffer``, ``require``, ``global``). Use this
      whenever the JS gates on ``typeof window === "undefined"`` or
      ``process.versions.node`` — common in anti-bot decoders that
      refuse to run outside a real browser. NOT a real DOM (no layout,
      no parser) — for that fall back to ``sandbox_eval_js``.

    For ``env="browser"`` you can also seed ``page_url`` (sets
    ``location.href`` / ``document.URL``) and ``user_agent`` (sets
    ``navigator.userAgent``).

    Returns ``{ok, result, result_type, result_size_chars, stdout,
    stderr, error, stack, elapsed_ms}``. ``result`` is the JSON-encoded
    return value (or a string for string returns); ``result_type``
    tells you which.
    """
    from argusai.core.reverse.js_python import run_js
    src = str(source or "")
    if path and not src:
        try:
            p = _resolve_session_input_path(api, kwargs, path)
            src = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": False, "error": f"failed to read path: {e}", "hint": "For large JS bundles, pass path instead of embedding source; write complex snippets to a file to avoid PowerShell quoting issues."}
    profile: dict[str, Any] = {}
    if profile_json:
        import json
        try:
            raw = json.loads(profile_json)
            profile = raw if isinstance(raw, dict) else {}
        except Exception:
            profile = {}

    return run_js(
        source=src,
        mode=str(mode or "script"),
        function_name=str(function_name or ""),
        args=list(args) if isinstance(args, list) else [],
        globals_=dict(globals) if isinstance(globals, dict) else {},
        inner_timeout_ms=_safe_int(inner_timeout_ms, 5000, 100, 60000),
        process_timeout_seconds=_safe_int(process_timeout_seconds, 30, 1, 600),
        env=str(env or "node").lower(),
        page_url=str(page_url or ""),
        user_agent=str(user_agent or ""),
        profile=profile,
        api=api,
        sandbox_root=_session_sandbox_root(api, kwargs),
    )


def run_js_function(
    api: Any,
    source: str = "",
    path: str = "",
    function_name: str = "",
    args: Any = None,
    globals: Any = None,
    inner_timeout_ms: int = 5000,
    process_timeout_seconds: int = 30,
    env: str = "node",
    page_url: str = "",
    user_agent: str = "",
    profile_json: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Shortcut for ``run_node_script(mode='call')`` — define the JS
    source, then call one named function. The most common pattern in
    reverse-engineering PoCs.

    Pass ``env="browser"`` when the source you're calling into checks
    for browser globals (``typeof window``, ``navigator.userAgent``,
    ``process.versions.node``). The stub environment makes those guards
    pass without rerouting to Playwright.

    Example: you read the deobfuscated bundle (859 KB JS) via
    ``read_text_file``, hand it to this tool as ``source``, then call
    ``entryFn`` with ``[{challengePack, authPayload}]`` — the returned
    JSON is the encrypted body you POST to the login endpoint.
    """
    from argusai.core.reverse.js_python import run_js_function as _do
    src = str(source or "")
    if path and not src:
        try:
            p = _resolve_session_input_path(api, kwargs, path)
            src = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"ok": False, "error": f"failed to read path: {e}", "hint": "For large JS bundles, pass path instead of embedding source; write complex snippets to a file to avoid PowerShell quoting issues."}
    profile: dict[str, Any] = {}
    if profile_json:
        import json
        try:
            raw = json.loads(profile_json)
            profile = raw if isinstance(raw, dict) else {}
        except Exception:
            profile = {}

    return _do(
        source=src,
        function_name=str(function_name or ""),
        args=list(args) if isinstance(args, list) else [],
        globals_=dict(globals) if isinstance(globals, dict) else {},
        inner_timeout_ms=_safe_int(inner_timeout_ms, 5000, 100, 60000),
        process_timeout_seconds=_safe_int(process_timeout_seconds, 30, 1, 600),
        env=str(env or "node").lower(),
        page_url=str(page_url or ""),
        user_agent=str(user_agent or ""),
        profile=profile,
        api=api,
        sandbox_root=_session_sandbox_root(api, kwargs),
    )


def crypto_reproduce(
    api: Any,
    algorithm: str = "",
    direction: str = "encrypt",
    key: str = "",
    iv: str = "",
    data: str = "",
    expected: str = "",
    key_format: str = "auto",
    iv_format: str = "auto",
    data_format: str = "auto",
    expected_format: str = "auto",
    output_format: str = "hex",
    no_padding: bool = False,
    aad: str = "",
    aad_format: str = "auto",
    **kwargs: Any,
) -> dict[str, Any]:
    """Reproduce a JS-side crypto operation in pure Python and verify
    against a golden vector.

    Supports ``xor | aes-cbc | aes-ecb | aes-ctr | aes-gcm | des-cbc |
    des-ecb | 3des-cbc | 3des-ecb | rc4 | hmac-sha256 | hmac-sha1 |
    hmac-sha512 | hmac-md5 | sha256 | sha1 | sha512 | md5``.

    Workflow:

    1. Identify the algorithm + parameters via static analysis or
       runtime trace.
    2. Capture ONE known input/output pair from the live JS (e.g. via
       ``browser_trace_crypto``).
    3. Call this tool with the JS's exact key/iv/input as ``key``,
       ``iv``, ``data`` and pass the captured ciphertext as ``expected``.
    4. If ``matched: true``, you have a verified Python re-implementation
       and the returned ``python_snippet`` is ready to paste into the
       final PoC. If ``matched: false``, ``match_hint`` tells you where
       the first diverging byte is — usually a key/IV encoding mismatch.

    Inputs accept hex, base64, base64url, or utf-8; the ``*_format``
    parameters force interpretation when auto-detection is ambiguous.
    """
    from argusai.core.security.crypto_reproduce import crypto_reproduce as _do

    return _do(
        algorithm=str(algorithm or ""),
        direction=str(direction or "encrypt"),
        key=str(key or ""),
        iv=str(iv or ""),
        data=str(data or ""),
        expected=str(expected or ""),
        key_format=str(key_format or "auto"),
        iv_format=str(iv_format or "auto"),
        data_format=str(data_format or "auto"),
        expected_format=str(expected_format or "auto"),
        output_format=str(output_format or "hex"),
        no_padding=bool(no_padding),
        aad=str(aad or ""),
        aad_format=str(aad_format or "auto"),
    )


def create_python_poc(
    api: Any,
    out_dir: str = "",
    title: str = "PoC",
    description: str = "",
    walkthrough: str = "",
    run_py: str = "",
    requirements: Any = None,
    helpers: Any = None,
    usage_args: str = "",
    overwrite: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Materialise a one-click Python PoC bundle on disk.

    Creates ``run.py``, ``requirements.txt``, ``README.md`` and an
    optional ``helpers/`` directory. The user runs the PoC with:

        cd <out_dir>
        pip install -r requirements.txt
        python run.py <usage_args>

    Args:
      out_dir: Target directory. Empty or relative values are resolved under
        the current assistant session sandbox. Absolute paths are treated as
        explicit user-selected external paths.
      run_py: The full ``run.py`` source. Required.
      requirements: List of pip packages (or newline-separated string).
      helpers: Optional dict ``{filename: content}`` for additional
        modules to write under ``helpers/``.
      walkthrough: Markdown-formatted step-by-step for the README.
      usage_args: Example CLI tail for the README.
      overwrite: If false, refuse to overwrite an existing ``run.py``.

    Prefer this over dumping multi-block code into the chat: the user
    gets a literally one-command-to-run artefact in their sandbox, and
    we can pair it with ``sandbox_reveal_in_folder`` so they see the
    files.
    """
    from argusai.core.security.python_poc import create_python_poc as _do

    try:
        target, root_kind, root_path = _resolve_session_output_path(api, kwargs, out_dir, default_name="poc")
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    session_id = str(kwargs.get("_session_id") or "")
    result = _do(
        out_dir=target,
        title=str(title or "PoC"),
        description=str(description or ""),
        walkthrough=str(walkthrough or ""),
        run_py=str(run_py or ""),
        requirements=requirements,
        helpers=helpers,
        usage_args=str(usage_args or ""),
        session_id=session_id,
        overwrite=bool(overwrite),
    )
    if result.get("ok"):
        result["root_kind"] = root_kind
        result["root"] = root_path
    return result


