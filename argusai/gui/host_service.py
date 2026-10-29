"""GUI bridge for the headless host OS service (``argusai serve``)."""

from __future__ import annotations

import sys
from typing import Any


class HostServiceMixin:
    """Expose install/start/stop/status for the settings automation panel."""

    def get_host_service_status(self) -> dict[str, Any]:
        from argusai.cli.service_install import (
            boot_policy_choices,
            load_host_service_prefs,
            privilege_probe,
            service_status,
        )

        try:
            status = service_status()
            prefs = load_host_service_prefs()
            status["ok"] = True
            status["platform"] = sys.platform
            status["boot_policy"] = str(
                prefs.get("boot_policy") or status.get("boot_policy") or default_boot_policy()
            )
            status["boot_policies"] = boot_policy_choices()
            status["cron_only"] = bool(
                prefs.get("cron_only")
                if "cron_only" in prefs
                else status.get("cron_only")
            )
            status["channel_host_held"] = self._host_service_channel_held()
            status.update(privilege_probe(boot_policy=status["boot_policy"]))
            try:
                from argusai.cli.macos_login_item import available_for_host

                status["login_item_available"] = available_for_host(
                    cron_only=bool(status.get("cron_only"))
                )
            except Exception:
                status["login_item_available"] = False
            status["notes"] = self._host_service_notes(status)
            return status
        except Exception as exc:
            from argusai.cli.service_install import privilege_probe

            priv = privilege_probe()
            return {
                "ok": False,
                "error": str(exc),
                "installed": False,
                "active": False,
                "platform": sys.platform,
                "boot_policies": boot_policy_choices(),
                **priv,
            }

    def set_host_service(
        self,
        enabled: bool = True,
        cron_only: bool = False,
        boot_policy: str = "",
        start_now: bool = True,
    ) -> dict[str, Any]:
        """Enable/disable the user OS service backing ``argusai serve run``."""
        from argusai.cli.service_install import (
            host_service_spec,
            install_service,
            normalize_boot_policy,
            save_host_service_prefs,
            stop_service,
            uninstall_alternate_host,
            uninstall_legacy_services,
            uninstall_service,
        )

        try:
            policy = normalize_boot_policy(boot_policy or default_boot_policy())
            if not enabled:
                try:
                    stop_service()
                except Exception:
                    pass
                host = uninstall_service(host_service_spec(cron_only=False))
                cron = uninstall_service(host_service_spec(cron_only=True))
                legacy = uninstall_legacy_services()
                save_host_service_prefs({"enabled": False})
                return {
                    "ok": True,
                    "enabled": False,
                    "host": host,
                    "cron_only": cron,
                    "legacy_removed": legacy,
                    "status": self.get_host_service_status(),
                }

            if self._host_service_channel_held() and start_now:
                # GUI already hosts channels — install but do not start, avoid lock fight.
                start_now = False
                deferred = True
            else:
                deferred = False

            spec = host_service_spec(cron_only=bool(cron_only))
            cleaned = uninstall_legacy_services() + uninstall_alternate_host(spec)
            receipt = install_service(
                spec,
                start=bool(start_now),
                boot_policy=policy,
            )
            save_host_service_prefs(
                {
                    "enabled": True,
                    "cron_only": bool(cron_only),
                    "boot_policy": policy,
                    "unit": spec.unit,
                }
            )
            return {
                "ok": True,
                "enabled": True,
                "started": bool(receipt.get("started")),
                "start_deferred": deferred,
                "receipt": receipt,
                "replaced": cleaned,
                "status": self.get_host_service_status(),
                "warning": "",
            }
        except Exception as exc:
            return self._host_service_fail(exc)

    def start_host_service(self) -> dict[str, Any]:
        from argusai.cli.service_install import start_service

        try:
            if self._host_service_channel_held():
                return {
                    "ok": False,
                    "error_code": "channel_host_held",
                    "error": "",
                    "status": self.get_host_service_status(),
                }
            receipt = start_service()
            return {"ok": True, **receipt, "status": self.get_host_service_status()}
        except Exception as exc:
            return self._host_service_fail(exc)

    def stop_host_service(self) -> dict[str, Any]:
        from argusai.cli.service_install import stop_service

        try:
            receipt = stop_service()
            return {"ok": True, **receipt, "status": self.get_host_service_status()}
        except Exception as exc:
            return self._host_service_fail(exc)

    def _host_service_fail(self, exc: Exception) -> dict[str, Any]:
        from argusai.cli.service_install import HostServiceError, classify_host_service_error

        code = getattr(exc, "code", "") or classify_host_service_error(str(exc))
        if not isinstance(exc, HostServiceError) and not code:
            code = "serve_err_unknown"
        return {
            "ok": False,
            "error": str(exc),
            "error_code": code or getattr(exc, "code", ""),
            "status": self.get_host_service_status(),
        }

    def _host_service_channel_held(self) -> bool:
        try:
            runtime = self._channels_runtime()
            if getattr(runtime, "_host_lock_file", None) is not None:
                return True
            adapters = getattr(runtime, "_adapters", None) or {}
            return bool(adapters)
        except Exception:
            return False

    @staticmethod
    def _host_service_notes(status: dict[str, Any]) -> list[str]:
        from argusai.cli.service_install import launch_environment_warnings

        notes: list[str] = []
        notes.extend(launch_environment_warnings())
        platform = str(status.get("platform") or sys.platform)
        policy = str(status.get("boot_policy") or "")
        if platform == "win32":
            if policy == "startup" and not status.get("needs_elevation"):
                notes.append(
                    "Windows startup trigger runs a headless host at boot before anyone signs in."
                )
            elif not status.get("cron_only"):
                notes.append(
                    "Windows logon writes a per-user Startup entry "
                    "(Settings → Apps → Startup) and opens ArgusAI in the tray."
                )
            else:
                notes.append(
                    "Windows default is at user logon. Choose “system startup” for pre-logon boot."
                )
        elif platform.startswith("linux"):
            if policy == "linger":
                notes.append(
                    "systemd user lingering enabled so the host can start without an active login."
                )
            else:
                notes.append(
                    "Linux user units follow your login session unless lingering is enabled."
                )
        elif platform == "darwin":
            kind = str(status.get("kind") or "")
            if status.get("requires_approval"):
                notes.append(
                    "Allow ArgusAI in System Settings → General → Login Items & Extensions."
                )
            elif kind == "login_item" or status.get("login_item_available"):
                notes.append(
                    "macOS Login Item: starts hidden after you log in. "
                    "Click the Dock icon to reopen. No administrator needed."
                )
            else:
                notes.append(
                    "This copy is not a packaged .app, so macOS uses a user LaunchAgent. "
                    "ArgusAI.app registers a Login Item instead."
                )
        if status.get("channel_host_held"):
            notes.append(
                "This window already hosts channels. Logon autostart is registered "
                "and will reopen this app; do not start a second headless process."
            )
        return notes

    def open_macos_login_items(self) -> dict[str, Any]:
        """Open System Settings → Login Items (macOS 13+)."""
        from argusai.cli.macos_login_item import open_login_items_settings

        return open_login_items_settings()


def default_boot_policy() -> str:
    if sys.platform == "win32":
        return "logon"
    if sys.platform.startswith("linux"):
        return "linger"
    return "logon"
