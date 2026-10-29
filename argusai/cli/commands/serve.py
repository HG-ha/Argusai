"""``argusai serve`` — headless host lifecycle (run / install / start / …)."""

from __future__ import annotations

import typer

from argusai.cli.commands._host import fail, ok_payload
from argusai.cli.locale import label
from argusai.cli.ui import get_console, set_json_mode

app = typer.Typer(
    name="serve",
    help="Headless host: channels + cron (OS service lifecycle).",
    rich_markup_mode="rich",
    no_args_is_help=True,
)


def run_serve(*, cron_only: bool = False, json_out: bool = False) -> None:
    """Foreground headless host."""
    if cron_only:
        from argusai.cli.commands.cron import run_cron_serve

        run_cron_serve(json_out=json_out)
        return
    from argusai.cli.commands.channels import run_channels_serve

    run_channels_serve(json_out=json_out)


@app.command("run")
def run_cmd(
    cron_only: bool = typer.Option(
        False,
        "--cron-only",
        help="Only run the cron scheduler (skip channel adapters).",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Print startup status as JSON then keep running."
    ),
) -> None:
    """Run the headless host in the foreground (Ctrl+C to stop)."""
    run_serve(cron_only=cron_only, json_out=json_out)


@app.command("install")
def install_cmd(
    cron_only: bool = typer.Option(
        False,
        "--cron-only",
        help="Install a cron-only service instead of full host.",
    ),
    boot: str = typer.Option(
        "",
        "--boot",
        help="Boot policy: Windows logon|startup; Linux user|linger; macOS logon.",
    ),
    no_start: bool = typer.Option(
        False, "--no-start", help="Register only; do not start immediately."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the service plan without installing."
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Register the host as a user OS service (Task Scheduler / systemd --user / LaunchAgent)."""
    from argusai.cli.service_install import (
        host_service_spec,
        install_service,
        normalize_boot_policy,
        service_plan,
        uninstall_alternate_host,
        uninstall_legacy_services,
    )

    set_json_mode(json_out)
    spec = host_service_spec(cron_only=cron_only)
    policy = normalize_boot_policy(boot) if boot else normalize_boot_policy(None)
    if dry_run:
        plan = service_plan(spec, boot_policy=policy)
        if json_out:
            ok_payload(plan, json_out=True)
            return
        get_console().print(plan)
        return
    try:
        cleaned = uninstall_legacy_services() + uninstall_alternate_host(spec)
        receipt = install_service(spec, start=not no_start, boot_policy=policy)
        if cleaned:
            receipt = {**receipt, "replaced": cleaned}
    except Exception as exc:
        fail(str(exc), json_out=json_out)
    if json_out:
        ok_payload(receipt, json_out=True)
        return
    name = receipt.get("name") or receipt.get("unit") or spec.unit
    get_console().print(f"[ok]{label('serve_msg_installed')}[/] {name}")
    if receipt.get("note"):
        get_console().print(f"[hud.dim]{receipt['note']}[/]")
    for warn in list(receipt.get("warnings") or []):
        get_console().print(f"[warn]{warn}[/]")
    if cleaned:
        get_console().print(
            f"[hud.dim]Replaced previous service entries: {', '.join(cleaned)}[/]"
        )


@app.command("boot")
def boot_cmd(
    policy: str = typer.Argument(
        ...,
        help="Boot policy: Windows logon|startup; Linux user|linger; macOS logon.",
    ),
    start: bool = typer.Option(
        False,
        "--start",
        help="Also start the service after updating the trigger.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Change autostart policy for an already-installed host service."""
    from argusai.cli.service_install import (
        host_service_spec,
        install_service,
        load_host_service_prefs,
        normalize_boot_policy,
        resolve_installed_host,
        service_status,
    )

    set_json_mode(json_out)
    installed = resolve_installed_host()
    if installed is None:
        fail(label("serve_msg_not_installed"), json_out=json_out)
    prefs = load_host_service_prefs()
    cron_only = bool(
        prefs.get("cron_only")
        if "cron_only" in prefs
        else installed.unit.endswith("-cron")
    )
    boot_policy = normalize_boot_policy(policy)
    try:
        receipt = install_service(
            host_service_spec(cron_only=cron_only),
            start=bool(start),
            boot_policy=boot_policy,
        )
    except Exception as exc:
        fail(str(exc), json_out=json_out)
    status = service_status()
    if json_out:
        ok_payload({"receipt": receipt, "status": status}, json_out=True)
        return
    get_console().print(
        f"[ok]{label('serve_msg_boot')}[/] {boot_policy}  ·  "
        f"{receipt.get('name') or receipt.get('unit')}"
    )
    if receipt.get("note"):
        get_console().print(f"[hud.dim]{receipt['note']}[/]")


@app.command("uninstall")
def uninstall_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Remove the host OS service (and any legacy split services)."""
    from argusai.cli.service_install import (
        HOST_SERVICE,
        host_service_spec,
        save_host_service_prefs,
        uninstall_legacy_services,
        uninstall_service,
    )

    set_json_mode(json_out)
    try:
        receipt = uninstall_service(HOST_SERVICE)
        cron_receipt = uninstall_service(host_service_spec(cron_only=True))
        legacy = uninstall_legacy_services()
        save_host_service_prefs({"enabled": False})
    except Exception as exc:
        fail(str(exc), json_out=json_out)
    removed_names: list[str] = []
    if receipt.get("removed"):
        removed_names.append(
            str(receipt.get("name") or receipt.get("unit") or HOST_SERVICE.unit)
        )
    if cron_receipt.get("removed"):
        removed_names.append("argusai-host-cron")
    removed_names.extend(legacy)
    if json_out:
        ok_payload(
            {
                "host": receipt,
                "cron_only": cron_receipt,
                "legacy_removed": legacy,
                "removed": removed_names,
            },
            json_out=True,
        )
        return
    if removed_names:
        get_console().print(
            f"[ok]{label('serve_msg_removed')}[/] {', '.join(removed_names)}"
        )
    else:
        get_console().print(f"[hud.dim]{label('serve_msg_none_registered')}[/]")


@app.command("start")
def start_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Start the installed host service."""
    from argusai.cli.service_install import start_service

    set_json_mode(json_out)
    try:
        receipt = start_service()
    except Exception as exc:
        fail(str(exc), json_out=json_out)
    if json_out:
        ok_payload(receipt, json_out=True)
        return
    get_console().print(
        f"[ok]{label('serve_msg_started')}[/] {receipt.get('name') or receipt.get('unit')}"
    )


@app.command("stop")
def stop_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Stop the installed host service."""
    from argusai.cli.service_install import stop_service

    set_json_mode(json_out)
    try:
        receipt = stop_service()
    except Exception as exc:
        fail(str(exc), json_out=json_out)
    if json_out:
        ok_payload(receipt, json_out=True)
        return
    get_console().print(
        f"[ok]{label('serve_msg_stopped')}[/] {receipt.get('name') or receipt.get('unit')}"
    )


@app.command("restart")
def restart_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Restart the installed host service."""
    from argusai.cli.service_install import restart_service

    set_json_mode(json_out)
    try:
        receipt = restart_service()
    except Exception as exc:
        fail(str(exc), json_out=json_out)
    if json_out:
        ok_payload(receipt, json_out=True)
        return
    get_console().print(
        f"[ok]{label('serve_msg_restarted')}[/] {receipt.get('name') or receipt.get('unit')}"
    )


@app.command("status")
def status_cmd(
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show whether the host service is installed / running."""
    from argusai.cli.service_install import service_status

    set_json_mode(json_out)
    receipt = service_status()
    if json_out:
        ok_payload(receipt, json_out=True)
        return
    if not receipt.get("installed"):
        get_console().print(
            f"[hud.dim]{label('serve_status_not_installed')}[/]  ·  "
            f"{label('serve_status_hint_install')}"
        )
        return
    state = (
        label("serve_status_active")
        if receipt.get("active")
        else str(receipt.get("state") or label("serve_msg_stopped"))
    )
    unit = receipt.get("name") or receipt.get("unit")
    mode = (
        label("serve_mode_cron_only")
        if receipt.get("cron_only")
        else label("serve_mode_full")
    )
    get_console().print(f"[ok]{state}[/]  {unit}  ({mode})")
