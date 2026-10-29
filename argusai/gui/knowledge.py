"""Knowledge-base CRUD + trash management.

All persistence lives in :class:`argusai.core.workspace.knowledge.KnowledgeBase`;
this mixin just adapts the API for the bridge (response envelope shape,
``{"ok": ..., "error": ..., ...}``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class KnowledgeMixin:
    """Bridge methods for the knowledge base."""

    def kb_list(
        self, include_superseded: bool = False, include_trashed: bool = False
    ) -> dict[str, Any]:
        try:
            entries = self.kb.list_entries(
                include_superseded=bool(include_superseded),
                include_trashed=bool(include_trashed),
            )
            return {"ok": True, "entries": entries, "root": str(self.kb.root)}
        except Exception as e:
            return {"ok": False, "error": str(e), "entries": []}

    def kb_get(self, entry_id: str) -> dict[str, Any]:
        try:
            entry = self.kb.get(str(entry_id or ""))
            if entry is None:
                return {"ok": False, "error": "not_found"}
            return {"ok": True, "entry": entry}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def kb_search(
        self,
        query: str = "",
        tags: list[str] | None = None,
        limit: int = 20,
        include_superseded: bool = False,
    ) -> dict[str, Any]:
        try:
            entries = self.kb.search(
                str(query or ""),
                tags=list(tags or []),
                limit=int(limit or 20),
                include_superseded=bool(include_superseded),
            )
            return {"ok": True, "entries": entries, "query": str(query or "")}
        except Exception as e:
            return {"ok": False, "error": str(e), "entries": []}

    def kb_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a brand-new knowledge entry from the UI."""
        try:
            data = payload or {}
            entry = self.kb.save(
                title=str(data.get("title") or "").strip(),
                body=str(data.get("body") or ""),
                tags=data.get("tags") or [],
                trigger_terms=data.get("trigger_terms") or [],
                applies_to=data.get("applies_to") or [],
                confidence=str(data.get("confidence") or "probable"),
                related=data.get("related") or [],
                task_id=str(data.get("task_id") or ""),
                session_id=str(data.get("session_id") or ""),
                supersedes=str(data.get("supersedes") or ""),
                reason=str(data.get("reason") or "用户手动新建"),
            )
            return {"ok": True, "entry": entry}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def kb_update(self, entry_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Edit an existing knowledge entry from the UI."""
        try:
            data = payload or {}
            updated = self.kb.update(
                str(entry_id or ""),
                title=data.get("title"),
                body=data.get("body"),
                tags=data.get("tags"),
                trigger_terms=data.get("trigger_terms"),
                applies_to=data.get("applies_to"),
                confidence=data.get("confidence"),
                related=data.get("related"),
                task_id=data.get("task_id"),
                session_id=str(data.get("session_id") or ""),
                reason=str(data.get("reason") or "用户在 UI 中编辑"),
            )
            if updated is None:
                return {"ok": False, "error": "not_found"}
            return {"ok": True, "entry": updated}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def kb_delete(self, entry_id: str, reason: str = "") -> dict[str, Any]:
        try:
            ok = self.kb.delete(str(entry_id or ""), reason=str(reason or "用户在 UI 中删除"))
            return {"ok": bool(ok)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def kb_restore(self, entry_id: str) -> dict[str, Any]:
        try:
            entry = self.kb.restore(str(entry_id or ""))
            if entry is None:
                return {"ok": False, "error": "not_found"}
            return {"ok": True, "entry": entry}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def kb_list_trash(self) -> dict[str, Any]:
        """List trashed entries with age/size — used by the cleanup UI."""
        try:
            entries = self.kb.list_trash()
            return {
                "ok": True,
                "entries": entries,
                "total_bytes": self.kb.trash_size_bytes(),
                "count": len(entries),
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "entries": [], "total_bytes": 0, "count": 0}

    def kb_suggest_cleanup(
        self, min_age_days: int = 30, max_results: int = 50
    ) -> dict[str, Any]:
        """Return trashed entries old enough to be reasonable purge candidates."""
        try:
            candidates = self.kb.suggest_cleanup(
                max_results=int(max_results or 50),
                min_age_days=int(min_age_days or 30),
            )
            return {"ok": True, "candidates": candidates, "count": len(candidates)}
        except Exception as e:
            return {"ok": False, "error": str(e), "candidates": [], "count": 0}

    def kb_purge_trash(
        self,
        older_than_days: int | None = None,
        ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Hard-delete trashed entries.

        - ``ids`` (preferred when present): purge only those specific entries.
        - ``older_than_days`` (when ``ids`` is empty): wipe trashed entries
          older than N days; ``None`` / ``0`` wipes everything in trash.

        Always returns ``{ok, removed, freed_bytes, kept}``.
        """
        try:
            if ids:
                removed: list[str] = []
                freed = 0
                for kid in ids:
                    safe = str(kid or "").strip()
                    if not safe:
                        continue
                    trash_md = Path(self.kb.root) / self.kb.TRASH_DIR / f"{safe}.md"
                    try:
                        size = trash_md.stat().st_size if trash_md.exists() else 0
                    except OSError:
                        size = 0
                    if self.kb.hard_delete(safe):
                        removed.append(safe)
                        freed += size
                return {"ok": True, "removed": removed, "freed_bytes": freed, "kept": 0}
            res = self.kb.purge_trash(
                older_than_days=int(older_than_days) if older_than_days is not None else None
            )
            return {"ok": True, **res}
        except Exception as e:
            return {"ok": False, "error": str(e), "removed": [], "freed_bytes": 0, "kept": 0}
