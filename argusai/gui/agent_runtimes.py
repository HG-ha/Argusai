"""pywebview bridge for local native/ACP agent runtimes."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any


def _new_runtime_id(payload: dict[str, Any], current: list[dict[str, Any]]) -> str:
    """Create an internal stable key without exposing it in the editor."""
    command_name = re.split(r"[\\/]", str(payload.get("command") or "").strip())[-1]
    command_stem = re.sub(r"\.(?:exe|cmd|bat|ps1|py)$", "", command_name, flags=re.I)
    label = str(payload.get("name") or "").strip() or command_stem
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:96]
    base = f"custom-{slug}" if slug else f"custom-acp-{uuid.uuid4().hex[:10]}"
    used = {str(item.get("id") or "") for item in current}
    if base not in used:
        return base
    while True:
        candidate = f"{base[:110]}-{uuid.uuid4().hex[:8]}"
        if candidate not in used:
            return candidate


class AgentRuntimesMixin:
    def list_agent_runtimes(self) -> dict[str, Any]:
        """Probe configured runtime commands without starting third-party CLIs."""
        from argusai.core.agent_runtimes import discover_agent_runtimes

        settings = self.settings_mgr.get()
        return discover_agent_runtimes(settings.agent_runtimes)

    def test_agent_runtime(self, runtime_id: str) -> dict[str, Any]:
        """Perform an explicit ACP initialize handshake for one runtime."""
        from argusai.core.agent_runtimes import runtime_configs, test_acp_runtime

        runtime_key = str(runtime_id or "").strip()
        runtime = next(
            (
                item
                for item in runtime_configs(self.settings_mgr.get().agent_runtimes)
                if item.id == runtime_key
            ),
            None,
        )
        if runtime is None:
            return {
                "ok": False,
                "error_code": "agent_runtime_unknown",
                "error": f"unknown agent runtime: {runtime_key or '(empty)'}",
            }
        if runtime.backend == "native":
            return {
                "ok": True,
                "runtime_id": runtime.id,
                "protocol_version": "native",
                "agent_info": {
                    "name": "argusai",
                    "title": "ArgusAI",
                },
                "agent_capabilities": {
                    "loadSession": True,
                    "argusaiTools": True,
                },
                "auth_methods": [],
            }
        from .ai_endpoints import _run_blocking

        return _run_blocking(lambda: test_acp_runtime(runtime))

    def list_session_models(
        self,
        session_id: str = "",
        refresh: bool = False,
        runtime_id: str = "",
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """List every model ``session_id`` can switch to, ACP or native.

        ``runtime`` covers the conversation the user just opened but has not
        sent anything in yet: nothing is persisted for it, so the caller sends
        the complete workspace/config snapshot. ``runtime_id`` remains for
        older frontends. Routing goes through ``_channel_ai_endpoint``, the same
        entry point the ``/model`` channel command uses, so the desktop picker
        and a remote chat cannot disagree about which models exist or are active.
        """
        result = self._channel_ai_endpoint("list", {
            "session_id": str(session_id or ""),
            "refresh": "1" if refresh else "",
            "runtime_id": str(runtime_id or ""),
            "runtime": dict(runtime or {}) if isinstance(runtime, dict) else {},
        })
        kind = str(result.get("kind") or "endpoint")
        if not result.get("ok"):
            return {
                "ok": False,
                "kind": kind,
                "code": str(result.get("code") or "acp_model_failed"),
                "probe_code": str(result.get("probe_code") or ""),
                "error": str(result.get("error") or ""),
            }
        models = list(result.get("endpoints") or [])
        return {
            "ok": True,
            "kind": kind,
            "session_id": str(session_id or ""),
            "models": models,
            "active": next((row for row in models if row.get("active")), {}),
            "runtime": result.get("runtime") or {},
            "model_source": str(result.get("model_source") or "session"),
            "stale": bool(result.get("stale")),
            "warning": str(result.get("warning") or ""),
        }

    def switch_session_model(
        self,
        session_id: str = "",
        index: int = -1,
        value: str = "",
        name: str = "",
    ) -> dict[str, Any]:
        """Switch the model for one conversation.

        For an ACP conversation the choice is stored on the session binding and
        ``run_acp_turn`` pushes it to the agent with
        ``session/set_config_option`` on the next turn, because an ACP session
        must exist before a config option can be set. For a native conversation
        it stores the owning endpoint and model on that conversation and uses
        a private failover chain. Global endpoint defaults remain unchanged.
        """
        result = self._channel_ai_endpoint("activate", {
            "session_id": str(session_id or ""),
            "index": int(index),
            "value": str(value or ""),
            "name": str(name or ""),
        })
        if not result.get("ok"):
            return {
                "ok": False,
                "kind": str(result.get("kind") or "endpoint"),
                "code": str(result.get("code") or "acp_model_failed"),
            }
        return {
            "ok": True,
            "kind": str(result.get("kind") or "endpoint"),
            "model": result.get("endpoint") or {},
            "runtime": result.get("runtime") or {},
        }

    def save_agent_runtime(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        from argusai.core.agent_runtimes import normalize_agent_runtimes

        payload = dict(data or {})
        result: dict[str, Any] = {}

        def _update(settings):
            current = normalize_agent_runtimes(settings.agent_runtimes)
            runtime_id = str(payload.get("id") or "").strip().lower()
            if not runtime_id:
                runtime_id = _new_runtime_id(payload, current)
                payload["id"] = runtime_id
            previous = next(
                (item for item in current if str(item.get("id") or "") == runtime_id),
                {},
            )
            merged = {**previous, **payload}
            if "env" not in payload and previous.get("env"):
                merged["env"] = previous["env"]
            existing = [
                item for item in current
                if str(item.get("id") or "") != runtime_id
            ]
            existing.append(merged)
            normalized = normalize_agent_runtimes(existing)
            saved = next(
                (item for item in normalized if item.get("id") == runtime_id),
                None,
            )
            if saved is None:
                raise ValueError("runtime configuration is invalid")
            settings.agent_runtimes = normalized
            result["runtime"] = saved
            return settings

        try:
            update = getattr(self.settings_mgr, "update", None)
            if callable(update):
                update(_update)
            else:
                self.settings_mgr.save(_update(self.settings_mgr.get()))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        saved = result["runtime"]
        return {"ok": True, "runtime": saved}

    def delete_agent_runtime(self, runtime_id: str) -> dict[str, Any]:
        from argusai.core.agent_runtimes import BUILTIN_AGENT_RUNTIMES, normalize_agent_runtimes

        runtime_key = str(runtime_id or "").strip().lower()
        if any(item.id == runtime_key for item in BUILTIN_AGENT_RUNTIMES):
            return {
                "ok": False,
                "error": "built-in runtime presets can be disabled but not deleted",
            }
        def _update(settings):
            current = normalize_agent_runtimes(settings.agent_runtimes)
            next_items = [
                item for item in current
                if str(item.get("id") or "") != runtime_key
            ]
            if len(next_items) == len(current):
                raise ValueError("runtime not found")
            settings.agent_runtimes = normalize_agent_runtimes(next_items)
            return settings

        try:
            update = getattr(self.settings_mgr, "update", None)
            if callable(update):
                update(_update)
            else:
                self.settings_mgr.save(_update(self.settings_mgr.get()))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "runtime_id": runtime_key}

    def choose_agent_project_directory(self, initial_dir: str = "") -> dict[str, Any]:
        """Pick a project directory for a new, otherwise empty conversation."""
        try:
            import webview

            window = getattr(self, "_window", None) or (webview.windows[0] if webview.windows else None)
            if window is None:
                return {"ok": False, "error": "no active window for folder dialog"}
            candidate = Path(str(initial_dir or "")).expanduser()
            while str(candidate) and not candidate.exists() and candidate != candidate.parent:
                candidate = candidate.parent
            if not candidate.is_dir():
                candidate = Path.home()
            picked = window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=str(candidate.resolve()),
            )
            if not picked:
                return {"ok": False, "cancelled": True}
            selected = picked[0] if isinstance(picked, (list, tuple)) else picked
            path = Path(str(selected)).expanduser().resolve()
            if not path.is_dir():
                return {"ok": False, "error": "selected project directory does not exist"}
            return {"ok": True, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def choose_agent_runtime_executable(self, initial_path: str = "") -> dict[str, Any]:
        """Choose a custom ACP executable without requiring a typed path."""
        try:
            import webview

            window = getattr(self, "_window", None) or (webview.windows[0] if webview.windows else None)
            if window is None:
                return {"ok": False, "error": "no active window for file dialog"}
            candidate = Path(str(initial_path or "")).expanduser()
            if candidate.is_file():
                initial_dir = candidate.parent
            elif candidate.is_dir():
                initial_dir = candidate
            else:
                initial_dir = Path.home()
            picked = window.create_file_dialog(
                webview.OPEN_DIALOG,
                directory=str(initial_dir.resolve()),
                file_types=(
                    "Executables (*.exe;*.cmd;*.bat;*.com;*.ps1)",
                    "All files (*.*)",
                ),
            )
            if not picked:
                return {"ok": False, "cancelled": True}
            selected = picked[0] if isinstance(picked, (list, tuple)) else picked
            path = Path(str(selected)).expanduser().resolve()
            if not path.is_file():
                return {"ok": False, "error": "selected runtime executable does not exist"}
            return {
                "ok": True,
                "path": str(path),
                "suggested_name": path.stem,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
