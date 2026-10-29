"""Cross-platform "show this path in the OS file manager" helper.

Why a dedicated module:

* ``session_sandbox.reveal_in_folder`` already does this, but is tied
  to the assistant session sandbox (it always runs ``resolve_in_sandbox``
  containment first). The reverse-engineering workspace lives in a
  different directory tree (``data_root/reverse_jobs/``) and the
  containment check must be done by the caller against that tree,
  not against an assistant sandbox root.

* The exporter / archive flows (``reverse_job_export``) also want to
  reveal the generated zip; centralising the platform branches here
  keeps Windows / macOS / Linux differences in one place.

Containment is the caller's responsibility — pass an already-resolved
absolute path that you have validated against the relevant root.

Windows note: do **not** launch Explorer via ``quiet_subprocess_kwargs()``
(``STARTF_USESHOWWINDOW`` + ``SW_HIDE`` / ``CREATE_NO_WINDOW``). Those
flags are for hiding console flash from CLI tools; on Explorer they make
the file-manager window open hidden or never appear — which looks exactly
like a dead "系统打开" button. Prefer ``os.startfile`` for directories, and a
plain ``explorer /select,`` for files.
"""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path
from typing import Any


def reveal_path(target: Path) -> dict[str, Any]:
    """Open ``target`` in the OS file manager (selecting it when possible).

    Returns ``{"ok": True, "path": str}`` on success or
    ``{"ok": False, "error": ...}`` on failure. The OS handler is
    launched out-of-process and not awaited — we deliberately do not
    block the GUI on Explorer / Finder startup.
    """
    if not isinstance(target, Path):
        target = Path(str(target))
    try:
        target = target.expanduser().resolve()
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"invalid path: {exc}"}
    if not target.exists():
        return {"ok": False, "error": "path not found", "path": str(target)}

    system = platform.system().lower()
    try:
        if system.startswith("win"):
            if target.is_file():
                # Two-arg form is the documented Explorer select syntax.
                # No quiet/hide flags — see module docstring.
                subprocess.Popen(
                    ["explorer", "/select,", str(target)],
                    close_fds=True,
                )
            else:
                # startfile is the most reliable "open this folder" on Windows
                # (shell association, no hidden-window STARTUPINFO traps).
                os.startfile(str(target))  # type: ignore[attr-defined]
        elif system == "darwin":
            args = ["open"]
            if target.is_file():
                args += ["-R", str(target)]
            else:
                args += [str(target)]
            subprocess.Popen(args, close_fds=True)
        else:
            folder = target if target.is_dir() else target.parent
            subprocess.Popen(["xdg-open", str(folder)], close_fds=True)
    except (OSError, FileNotFoundError) as exc:
        return {"ok": False, "error": str(exc), "path": str(target)}
    return {"ok": True, "path": str(target)}


__all__ = ["reveal_path"]
