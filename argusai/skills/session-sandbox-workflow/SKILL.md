---
name: session-sandbox-workflow
description: Use when the user asks to create files, save output, write scripts, run Python/Node/shell code, inspect the assistant session sandbox, or install/repair a managed sandbox runtime.
trigger-keywords:
  - "\\b(sandbox_write_file|sandbox_patch_file|sandbox_read_file|sandbox_stat_file|sandbox_grep_file|run_python_script|run_shell_script|run_python_snippet|run_shell_snippet|shell_session_start|shell_session_exec)\\b"
  - "\\b(session sandbox|managed runtime|runtime_install|wabt|shell session)\\b"
  - "沙盒|写入文件|运行.*(python|shell|脚本)|沙盒运行时|安装\\s*WABT|交互式.*shell"
trigger-tools:
  - sandbox_write_file
  - sandbox_patch_file
  - sandbox_read_file
  - sandbox_stat_file
  - sandbox_grep_file
  - sandbox_open_file
  - sandbox_reveal_in_folder
  - run_python_snippet
  - run_python_script
  - run_shell_snippet
  - run_shell_script
  - run_host_python_snippet
  - run_host_python_script
  - shell_session_start
  - shell_session_exec
  - shell_session_write
  - shell_session_list
  - shell_session_read
  - shell_session_stop
  - sandbox_pip_install
  - runtime_install
  - export_workspace_bundle
---

# Session Sandbox Workflow

Use the session file sandbox for AI-generated files and scripts. The sandbox is isolated per assistant session under the configured session storage directory as `<session_id>.sandbox`.

## Sandbox vs codebase (pick correctly)

| Goal | Use |
|---|---|
| Edit / test an **existing user repository** | `codebase_scope_set` + `codebase_patch_file` / `codebase_write_file` + `codebase_run_shell_*` (see `software-development`) |
| Greenfield script, PoC, lab, throwaway experiment | This sandbox workflow |
| Escape sandbox to touch the repo via absolute paths | **Forbidden** — use codebase tools instead |

Never use `run_shell_snippet` / `run_python_script` with host absolute paths to modify a user repo. That is blocked and is the wrong tool.

## Principles

- Keep **sandbox-tier** generated files inside the current session sandbox. Do not write to the project directory, user home, system directories, or arbitrary paths via sandbox tools.
- **Default tier**: run all code in the session sandbox `.venv` via `run_python_snippet` / `run_python_script` / shell tools. Do not pass `python_env` to sandbox tools.
- Python and Node use shared, verified bases under `data_root/tools/runtimes`. Python creates one `.venv` per session; Node keeps its shared binary and packages but uses a per-session cwd, HOME, TEMP, and npm cache.
- If a managed runtime is missing or damaged, let the unified confirmation card install/repair it and continue the original tool call. `runtime_install(runtime='python'|'node'|'wabt', force=...)` is the explicit repair path and always requires confirmation. WABT is a managed runtime (Settings → Sandbox runtimes), not a reverse-toolchain tool — never use `reverse_toolchain_install` for it.
- Use `discover_python_environments` only for diagnostics or when filling `python_env` on **host escalation** tools — not to bypass sandbox execution silently.
- Prefer `run_python_snippet` for short one-off Python logic.
- Prefer `sandbox_write_file` for **new** files; prefer `sandbox_patch_file` for **small edits** to existing files (exact `old_string` → `new_string`).
- Prefer `sandbox_write_file` followed by `run_python_script` or `run_shell_script` for longer code, reusable scripts, or code the user may want to inspect.
- Use shell snippets only when shell syntax is necessary. Prefer Python for portable text/file/data processing.
- For evidence-producing scripts or PoCs, attach important outputs to the active case with `artifact_add` / `evidence_add` instead of leaving them only in the sandbox.
- Before execution, briefly state what will run, which interpreter or shell will be used, the sandbox working directory, timeout, and expected effect. Wait for the permission card.
- Execution tools require explicit user confirmation every time. Do not rely on auto-allow for script execution.

## Sandbox boundary — the rule that catches AI most often

Every **sandbox-tier** execution tool (`run_python_script`, `run_python_snippet`, `run_shell_script`, `run_shell_snippet`) runs INSIDE the per-session sandbox:

- `path` arguments must resolve under the sandbox root. Absolute paths to the project repo, the user's home, or `C:\Windows` are REJECTED with `error_code=path_outside_sandbox`.
- The shell snippet's working directory is the sandbox root, and the sandbox venv (`.venv/Scripts` or `.venv/bin`) is on `PATH` FIRST. A bare `python script.py` uses the sandbox venv, not the host Python that launched ArgusAI.
- **Hard-coding host `python.exe` paths or `py -3` in shell snippets is blocked.** Do not try to work around this.
- **Trying to use `run_shell_snippet` to escape `run_python_script`'s sandbox check does not work.** Both tools run in the same sandbox cwd with the same PATH.

If you find yourself reaching for a host-side interpreter, stop and pick the right replacement:

| Situation | Do this |
|---|---|
| You want to tweak a few lines in an existing sandbox file. | `sandbox_grep_file` or `sandbox_read_file(offset=<line>, limit=40)` → copy from **`content_raw`** → `sandbox_patch_file`. Do **not** rewrite the whole file unless the change is large. |
| You need to inspect a large sandbox file. | `sandbox_stat_file` first; if `large_file`, `sandbox_grep_file` then read only the hit window. For minified JS use `grep` mode=`context` + `read_mode='chars'`. |
| You want to edit/run a script that lives in the user's repo. | Set `codebase_scope_set` then use `codebase_patch_file` / `codebase_run_shell_*` / `codebase_run_python_*` (see `software-development`). Do **not** copy the whole repo into the sandbox. |
| You want a one-off Python computation. | `run_python_snippet(code=<inline>)` — never write a temp file by hand. |
| Sandbox failed because the managed runtime lacks a host-only GUI/OS capability. | First show sandbox failure evidence. Then call `run_host_python_snippet` or `run_host_python_script` with `reason` + `sandbox_attempt_summary`. Optional `python_env` from `discover_python_environments`. User must confirm every time. |
| You need tkinter or another host GUI facility. | Prefer HTML/Markdown when GUI is unnecessary. Try the sandbox first; escalate only after showing the sandbox failure. |
| You need third-party Python packages (requests, pygame, …). | `session_sandbox_status` → `sandbox_pip_install` → `run_python_script`. Never `run_host_python_*` for pip. |
| User wants a standalone `.exe` / PyInstaller bundle. | `sandbox_pip_install pyinstaller` + `run_python_script` in the sandbox `.venv`. If that fails, print a manual `python -m venv` recipe for the user — **never** PyInstaller on host/Conda. |
| pygame / GUI shows Chinese as empty squares (tofu). | **Do not** use `pygame.font.Font(None, …)` for CJK text — the built-in font has no Chinese glyphs. Copy a `.ttf` into the sandbox (e.g. `assets/msyh.ttf`) and use `pygame.font.Font("assets/msyh.ttf", size)`. On Windows you may use `SysFont` with a real family name (e.g. `"microsoftyahei"`), not `Font(None)`. Downgrading sandbox Python alone does not fix missing glyphs. |
| Managed Python/Node/WABT is missing or damaged. | Approve the install card, or use Settings → Sandbox runtimes / `runtime_install(runtime='python'|'node'|'wabt', force=true)`. Do not substitute system Python/Node, and do not call `reverse_toolchain_install` for WABT. |

## Managed runtime installation

- Downloads use pinned versions and SHA-256 trust anchors. A checksum or lockfile integrity mismatch stops immediately; it never falls through to another mirror.
- Custom Python/Node/npm mirrors are configured in Settings and are tried before built-in fallbacks.
- Installation uses a cross-process lock, staging directory, and atomic replacement, so a failed repair does not pollute a healthy runtime.
- Offline use works only after the required shared runtime has been installed once. Existing installations are reused after restart.
- `discover_python_environments` is diagnostic only. System Python is reserved for explicit `run_host_python_*` escalation and is never a normal sandbox fallback.

## Safety

- Allowed file types include `txt`, `md`, `json`, `html`, `py`, `ps1`, `bat`, `cmd`, and `sh`.
- Never attempt path traversal, absolute paths, drive paths, UNC paths, or hidden output paths.
- Do not generate destructive scripts that delete user/system data, modify startup/security settings, format drives, or download and execute remote code.
- Keep timeouts small by default. Use 30 seconds unless the task clearly needs more, and never exceed the tool limit.

## Installing Python packages inside snippets

When a sandbox snippet must install a Python package, prefer **`sandbox_pip_install`** for one or more packages. Inside a snippet you may also use:

```python
result = subprocess.run(
    [sys.executable, "-m", "pip", "install", pkg],
    capture_output=True,
    text=True,
    timeout=180,
)
if result.returncode != 0:
    print(result.stdout + result.stderr)
```

If pip fails, call **`session_sandbox_status`** first and follow its hint (repair managed Python, reset `.venv`, etc.). Do **not** escalate to `run_host_python_*` for pip or PyInstaller.

If venv creation fails with **`0xC0000135` / `3221225781`**, managed Python could not load a required DLL. Repair Python in Settings → Sandbox runtimes, restart ArgusAI, run **`sandbox_reset_files`**, and retry.

Do not use `subprocess.check_call` for pip installs because it hides the useful stderr in the tool result. Do not add `-q` / `--quiet`; failures need visible output. If the snippet returns `lint_warnings`, fix the anti-pattern before retrying.
## Windows host + remote Linux (SSH)

`run_shell_snippet` runs on the **local ArgusAI machine**. On Windows:

- Prefer local PowerShell for local work. Use `shell="bash"` only when Git Bash is available (ArgusAI prefers real Git `bash.exe` over the WSL Store stub).
- **Never** paste bash heredoc / multi-line Linux scripts into PowerShell and hope quoting survives.
- For remote Linux, pick one of these patterns:
  1. `shell_session_start` with `ssh -T user@host`, then `shell_session_exec` for each remote command (best for multi-step ops).
  2. `sandbox_write_file` a `.sh` (LF endings), `scp` it, then `ssh -n -o BatchMode=yes user@host 'bash ~/script.sh'`.
  3. Single-line probes only: `ssh -n -o BatchMode=yes user@host 'cmd'`.
- Always detach ssh stdin (`-n` or `</dev/null`). PowerShell pipelines that leave stdin open commonly hang ssh until timeout.
- Do not base64-wrap and re-escape the same script through PowerShell as a first resort — fix the transport pattern instead.

## Long-running scripts and timeouts

Choose the smallest timeout that matches the work:

- simple one-off probes: 30-60 seconds;
- page crawls or fuzzers: 120-300 seconds;
- blind SQLi extraction or brute-force loops: 600-1800 seconds.

If a run times out and `partial_output=true`, inspect stdout/stderr first. Retry only when the missing tail is necessary, and narrow scope, widen timeout, or split into checkpointed batches.

**GUI apps / long-lived servers** (Flet, Tk, local HTTP servers, watchers):

- Prefer **`shell_session_start`** for multi-step interactive work: `exec` more commands, `read` the log (look for `__ARGUSAI_CMD_DONE__`), `stop` when finished. This is stdin-pipe interactive (not a full PTY — no vim/TUI).
- For a single long-lived GUI/server process, `run_*` with **`background=true`** returns `ok=true`, `detached=true`, `pid`, and a **oneshot** `session_id` (list/read/stop only — cannot exec). Prefer this over `start` / `pythonw` / `start /b`.
- **Single-instance discipline**: before relaunching the same GUI after a code change, call `shell_session_list` and `shell_session_stop` on the previous oneshot. Runtime also auto-stops a prior oneshot with the same `instance_key` (e.g. same `cd … & python main.py`), but you must still treat lifecycle as your job — do not stack duplicate windows for the user to clean up.
- After launch, verify with `desktop_observe` (prefer `hwnd` window screenshot / `desktop_focus_window`) instead of blindly launching again.
- If a GUI would block an interactive shell, use `Start-Process` inside `shell_session_exec`, or launch GUI as oneshot `background=true` and keep a separate interactive session for ops.
- A foreground timeout on a process that already opened a window is **not** a launch failure — do not re-launch; use `background=true` or `shell_session_*` instead.

Keep inner loops inside one script tool call when possible. For byte-by-byte extraction, enumeration, or credential checks, one script with progress output is better than many agent-round tool calls.

## Deliverables and opening files

When the user asks to hand off reports, PoC code, notes, evidence, or a whole working directory, use `export_workspace_bundle`. Inspect the sandbox first, pass the exact `sandbox_paths` when quality matters, and set `create_zip=true` when the user wants a compressed package.

When the user asks to see a file you produced, call `sandbox_open_file(path=...)`. For a folder or file location, use `sandbox_reveal_in_folder`. Do not paste an absolute path and leave the user to open it manually.

## After Execution

Summarize:

- File path or snippet path in the sandbox.
- Interpreter/shell and arguments.
- Exit code and timeout status.
- Key stdout/stderr lines.
- Where the audit record was written, if relevant.
