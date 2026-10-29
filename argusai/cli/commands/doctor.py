"""``argusai doctor`` — environment / runtime health table."""

from __future__ import annotations

from typing import Any

import typer

from argusai.cli.ui import emit_result, get_console, set_json_mode, status_table


def doctor(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Probe shells, managed runtimes, browser, and channel config."""
    set_json_mode(json_out)
    rows: list[tuple[str, str, str]] = []
    payload: dict[str, Any] = {"ok": True, "checks": []}

    # Platform / version
    import platform

    from argusai import __version__

    rows.append(("argusai", "ok", f"v{__version__} · {platform.system()} {platform.release()}"))
    payload["checks"].append({"name": "argusai", "status": "ok", "detail": rows[-1][2]})

    # Shells
    try:
        from argusai.core.workspace.session_sandbox import discover_shell_environments, find_bash_executable

        shells = discover_shell_environments()
        available = [s for s in shells.get("shells", []) if s.get("available")]
        names = ", ".join(str(s.get("name")) for s in available) or "(none)"
        bash = find_bash_executable()
        detail = names
        if bash:
            detail += f" · bash={bash}"
        status = "ok" if available else "warn"
        rows.append(("shells", status, detail))
        payload["checks"].append({"name": "shells", "status": status, "detail": detail, "bash": bash})
    except Exception as exc:
        rows.append(("shells", "fail", str(exc)))
        payload["checks"].append({"name": "shells", "status": "fail", "detail": str(exc)})
        payload["ok"] = False

    # Managed runtimes
    for runtime in ("python", "node", "wabt"):
        try:
            from argusai.core.managed_runtimes import runtime_status

            st = runtime_status(runtime)
            installed = bool(st.get("installed"))
            healthy = bool(st.get("healthy"))
            detail = str(
                st.get("version")
                or st.get("status")
                or st.get("error")
                or st.get("path")
                or ""
            )[:120]
            if installed and healthy:
                status = "ok"
            elif installed:
                status = "warn"
                detail = detail or "installed but unhealthy"
            else:
                status = "warn"
                detail = detail or "not installed"
            rows.append((f"runtime:{runtime}", status, detail))
            payload["checks"].append(
                {"name": f"runtime:{runtime}", "status": status, "detail": detail, "raw": st}
            )
        except Exception as exc:
            rows.append((f"runtime:{runtime}", "warn", str(exc)[:120]))
            payload["checks"].append(
                {"name": f"runtime:{runtime}", "status": "warn", "detail": str(exc)}
            )

    # Browser
    try:
        from argusai.core.browser.browser_selfcheck import self_check_browser

        report = self_check_browser()
        ok = bool(report.get("ok"))
        detail = str(report.get("summary") or report.get("error") or "playwright")[:160]
        status = "ok" if ok else "warn"
        rows.append(("browser", status, detail))
        payload["checks"].append({"name": "browser", "status": status, "detail": detail, "raw": report})
        if not ok:
            payload["ok"] = False
    except Exception as exc:
        rows.append(("browser", "warn", str(exc)[:120]))
        payload["checks"].append({"name": "browser", "status": "warn", "detail": str(exc)})

    # Channels config presence (no network)
    try:
        from argusai.core.settings import SettingsManager

        settings = SettingsManager().get()
        channels = getattr(settings, "channels", None) or getattr(settings, "communication_channels", None)
        count = 0
        if isinstance(channels, dict):
            count = len(channels)
        elif isinstance(channels, list):
            count = len(channels)
        else:
            # Fall back to channel store if settings shape differs.
            try:
                from argusai.core.channels.store import ChannelStore
                from argusai.core.paths import PathManager

                store = ChannelStore(PathManager().channels_dir)
                count = len(store.list_channels())
            except Exception:
                count = 0
        status = "ok" if count else "warn"
        detail = f"{count} configured" if count else "none configured"
        rows.append(("channels", status, detail))
        payload["checks"].append({"name": "channels", "status": status, "detail": detail})
    except Exception as exc:
        rows.append(("channels", "warn", str(exc)[:120]))
        payload["checks"].append({"name": "channels", "status": "warn", "detail": str(exc)})

    if json_out:
        emit_result(payload)
        return
    get_console().print(status_table("ArgusAI doctor", rows))
    raise typer.Exit(0 if payload.get("ok") else 1)
