"""Live UI context pushed from the Vue shell (route, overlays, page hints)."""

from __future__ import annotations

import time
from threading import Lock
from typing import Any


class UiContextMixin:
    """Store the latest UI snapshot from the embedded webview."""

    def _ensure_ui_context(self) -> None:
        if not hasattr(self, "_ui_context_lock"):
            self._ui_context_lock = Lock()
            self._ui_context: dict[str, Any] = {}

    def set_ui_context(self, data: dict[str, Any] | None = None) -> dict[str, Any]:
        self._ensure_ui_context()
        payload = dict(data or {})
        payload["received_at"] = time.time()
        with self._ui_context_lock:
            self._ui_context = payload
        return {"ok": True}

    def get_ui_context(self) -> dict[str, Any]:
        self._ensure_ui_context()
        with self._ui_context_lock:
            return dict(self._ui_context)
