"""Named session vault for multi-role AI workflows."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class SessionVault:
    """File-backed named sessions under ``<data_root>/session_vault``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        clean = str(name or "").strip()
        if not SESSION_ID_RE.match(clean):
            raise ValueError("invalid session name")
        return self.root / f"{clean}.json"

    def save(
        self,
        name: str,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        scope: list[str] | None = None,
        role: str = "",
        source: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        path = self._path(name)
        data = {
            "name": str(name).strip(),
            "role": str(role or name).strip(),
            "headers": {str(k): str(v) for k, v in (headers or {}).items()},
            "cookies": {str(k): str(v) for k, v in (cookies or {}).items()},
            "scope": [str(s).strip() for s in (scope or []) if str(s).strip()],
            "source": str(source or ""),
            "notes": str(notes or "")[:2000],
            "updated_at": _now_iso(),
        }
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                data["created_at"] = old.get("created_at") or data["updated_at"]
            except Exception:
                data["created_at"] = data["updated_at"]
        else:
            data["created_at"] = data["updated_at"]
        _atomic_write(path, data)
        return data

    def get(self, name: str) -> dict[str, Any] | None:
        path = self._path(name)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({
                "name": data.get("name"),
                "role": data.get("role"),
                "scope": data.get("scope") or [],
                "has_authorization": "Authorization" in (data.get("headers") or {}) or "authorization" in (data.get("headers") or {}),
                "cookie_names": sorted((data.get("cookies") or {}).keys()),
                "source": data.get("source"),
                "updated_at": data.get("updated_at"),
            })
        return out

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        return False


def headers_from_session(session: dict[str, Any] | None) -> dict[str, str]:
    if not session:
        return {}
    headers = {str(k): str(v) for k, v in (session.get("headers") or {}).items()}
    cookies = session.get("cookies") or {}
    if cookies and "Cookie" not in headers and "cookie" not in {k.lower(): v for k, v in headers.items()}:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return headers
