"""Per-user knowledge base ("我的知识库").

Stores AI-distilled, cross-session reusable knowledge as plain Markdown files
with YAML frontmatter. Each entry has a stable ``id`` so the assistant can
revisit and update its own notes. An ``index.json`` is maintained alongside
the markdown files for fast keyword search.

Design choices:

* Single file per entry — in-place updates bump ``version`` and append to
  the entry's own ``changelog`` block in frontmatter. We deliberately do
  NOT keep version snapshot files; the user can ``git init`` the knowledge
  directory if they want full history.
* ``supersedes`` / ``superseded_by`` give AI a way to mark a structurally
  different rewrite without losing the old entry. ``kb_search`` filters
  superseded entries by default but ``kb_read`` can still load them.
* Deletes are soft — the file moves into ``_trash/`` so users (and the AI)
  can restore. Hard wipe is a manual job.
"""

from __future__ import annotations

import json
import logging
import random
import re
import string
import time as _time
from pathlib import Path
from typing import Any, Iterable

import yaml

log = logging.getLogger("argusai.knowledge")


CONFIDENCE_VALUES = {"confirmed", "probable", "hypothesis", "experimental"}


def _now_iso() -> str:
    return _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime())


def _new_id() -> str:
    date = _time.strftime("%Y%m%d", _time.localtime())
    rand = "".join(random.choices(string.ascii_letters + string.digits, k=4))
    return f"kb-{date}-{rand}"


def _slug_tags(tags: Iterable[Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        t = str(raw or "").strip().lower()
        if not t:
            continue
        # Keep tag values URL/filename safe.
        t = re.sub(r"[^a-z0-9._-]+", "-", t).strip("-")
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _str_list(values: Iterable[Any], max_items: int = 32, max_len: int = 200) -> list[str]:
    out: list[str] = []
    for v in values or []:
        s = str(v or "").strip()
        if not s:
            continue
        if len(s) > max_len:
            s = s[:max_len]
        out.append(s)
        if len(out) >= max_items:
            break
    return out


def _normalize_confidence(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in CONFIDENCE_VALUES else "probable"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse ``---\\n<yaml>\\n---\\n<body>``.

    Falls back to ``({}, full_text)`` when frontmatter is missing/malformed so
    a user-edited file is never lost.
    """
    if not text.startswith("---"):
        return {}, text
    end_match = re.search(r"^---\s*$", text, flags=re.MULTILINE)
    if not end_match:
        return {}, text
    # ``end_match`` is the OPENING marker. Look for the closing one after
    # the first newline.
    body_start = text.find("\n", 3) + 1
    rest = text[body_start:]
    closing = re.search(r"^---\s*$", rest, flags=re.MULTILINE)
    if not closing:
        return {}, text
    fm_text = rest[: closing.start()]
    body = rest[closing.end():].lstrip("\n")
    try:
        meta = yaml.safe_load(fm_text) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}
    return meta, body


def _build_file_text(meta: dict[str, Any], body: str) -> str:
    fm = yaml.safe_dump(meta, allow_unicode=True, default_flow_style=False, sort_keys=False)
    return f"---\n{fm}---\n\n{body.strip()}\n"


# Optional UI live-push hook (installed by the gui layer). Core stays
# UI-agnostic; default no-op. The KB panel has no poll, so agent/tool writes
# would otherwise only show on remount.
_UI_NOTIFY = None


def set_ui_notify(callback) -> None:
    global _UI_NOTIFY
    _UI_NOTIFY = callback


class KnowledgeBase:
    """File-backed knowledge base.

    Thread-safe-ish: filesystem operations are atomic-enough for desktop use
    (write to a temp file then ``os.replace``). The in-memory ``_index`` is
    refreshed on every public read so concurrent writers from another
    process (e.g. user hand-editing a file) are picked up on next access.
    """

    INDEX_FILENAME = "index.json"
    TRASH_DIR = "_trash"

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / self.TRASH_DIR).mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / self.INDEX_FILENAME
        self._index: dict[str, dict[str, Any]] = {}
        self._index_mtime: float = 0.0
        self._load_index()

    # -- index --------------------------------------------------------------

    def _load_index(self) -> None:
        if not self._index_path.exists():
            self._index = {}
            self._scan_and_rebuild_index()
            return
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._index = {k: v for k, v in data.items() if isinstance(v, dict)}
            else:
                self._index = {}
            self._index_mtime = self._index_path.stat().st_mtime
        except Exception as e:
            log.warning("Failed to load %s, rebuilding from disk: %s", self._index_path, e)
            self._index = {}
            self._scan_and_rebuild_index()

    def _scan_and_rebuild_index(self) -> None:
        for md in self.root.glob("kb-*.md"):
            try:
                meta, _body = _split_frontmatter(md.read_text(encoding="utf-8"))
            except Exception:
                continue
            kid = str(meta.get("id") or md.stem)
            self._index[kid] = self._summary_from_meta(meta, md.name)
        self._save_index()

    def _save_index(self) -> None:
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._index, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(self._index_path)
        try:
            self._index_mtime = self._index_path.stat().st_mtime
        except OSError:
            pass
        cb = _UI_NOTIFY
        if cb is not None:
            try:
                cb("knowledge", {})
            except Exception:
                pass

    def _maybe_reload(self) -> None:
        """Pick up out-of-band edits (user opened the .md in an editor)."""
        try:
            mtime = self._index_path.stat().st_mtime
        except OSError:
            mtime = 0
        if mtime != self._index_mtime:
            self._load_index()

    def _summary_from_meta(self, meta: dict[str, Any], filename: str) -> dict[str, Any]:
        return {
            "id": meta.get("id"),
            "title": meta.get("title") or "(untitled)",
            "tags": list(meta.get("tags") or []),
            "trigger_terms": list(meta.get("trigger_terms") or []),
            "applies_to": list(meta.get("applies_to") or []),
            "confidence": meta.get("confidence") or "probable",
            "version": int(meta.get("version") or 1),
            "created_at": meta.get("created_at") or "",
            "updated_at": meta.get("updated_at") or meta.get("created_at") or "",
            "task_id": meta.get("task_id") or "",
            "supersedes": meta.get("supersedes") or "",
            "superseded_by": meta.get("superseded_by") or "",
            "related": list(meta.get("related") or []),
            "path": filename,
        }

    # -- file IO -----------------------------------------------------------

    def _entry_path(self, entry_id: str) -> Path:
        # Always derive filename from id — keeps file/url/index in sync.
        return self.root / f"{entry_id}.md"

    def _read_entry(self, entry_id: str, *, include_trashed: bool = False) -> tuple[dict[str, Any], str, Path] | None:
        p = self._entry_path(entry_id)
        if not p.exists():
            if include_trashed:
                tp = self.root / self.TRASH_DIR / f"{entry_id}.md"
                if tp.exists():
                    p = tp
                else:
                    return None
            else:
                return None
        text = p.read_text(encoding="utf-8")
        meta, body = _split_frontmatter(text)
        if not meta.get("id"):
            meta["id"] = entry_id
        return meta, body, p

    def _write_entry(self, meta: dict[str, Any], body: str, path: Path | None = None) -> Path:
        out = path or self._entry_path(str(meta.get("id")))
        text = _build_file_text(meta, body)
        tmp = out.with_suffix(".md.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(out)
        return out

    # -- public API --------------------------------------------------------

    def list_entries(self, *, include_superseded: bool = False, include_trashed: bool = False) -> list[dict[str, Any]]:
        self._maybe_reload()
        out: list[dict[str, Any]] = []
        for kid, summary in self._index.items():
            if not include_superseded and summary.get("superseded_by"):
                continue
            out.append({**summary, "id": kid})
        if include_trashed:
            trash_dir = self.root / self.TRASH_DIR
            if trash_dir.exists():
                for md in trash_dir.glob("kb-*.md"):
                    try:
                        meta, _body = _split_frontmatter(md.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    summary = self._summary_from_meta(meta, md.name)
                    summary["trashed"] = True
                    out.append({**summary, "id": meta.get("id") or md.stem})
        out.sort(key=lambda x: x.get("updated_at") or x.get("created_at") or "", reverse=True)
        return out

    def get(self, entry_id: str, *, include_trashed: bool = True) -> dict[str, Any] | None:
        rec = self._read_entry(entry_id, include_trashed=include_trashed)
        if rec is None:
            return None
        meta, body, p = rec
        is_trashed = p.parent.name == self.TRASH_DIR
        return {
            **self._summary_from_meta(meta, p.name),
            "id": meta.get("id") or entry_id,
            "body": body,
            "changelog": list(meta.get("changelog") or []),
            "trashed": is_trashed,
            "raw_meta": meta,
        }

    def search(self, query: str, *, tags: list[str] | None = None, limit: int = 8, include_superseded: bool = False) -> list[dict[str, Any]]:
        """Lightweight keyword search over the index (title + tags + applies_to + trigger_terms)."""
        self._maybe_reload()
        q = (query or "").strip().lower()
        wanted_tags = set(_slug_tags(tags or []))
        terms = [t for t in re.split(r"[\s,]+", q) if t]

        scored: list[tuple[float, dict[str, Any]]] = []
        for kid, summary in self._index.items():
            if not include_superseded and summary.get("superseded_by"):
                continue
            entry_tags = set(_slug_tags(summary.get("tags") or []))
            if wanted_tags and not (wanted_tags & entry_tags):
                # When tags is explicitly given, treat it as a hard filter.
                continue

            hay_parts = [
                str(summary.get("title") or ""),
                " ".join(summary.get("tags") or []),
                " ".join(summary.get("trigger_terms") or []),
                " ".join(summary.get("applies_to") or []),
            ]
            hay = " ".join(hay_parts).lower()
            score = 0.0
            if q and q in hay:
                score += 5.0
            for term in terms:
                if term and term in hay:
                    score += 2.0 if len(term) > 3 else 1.0
            # Tag overlap boost when the caller didn't pre-filter.
            if wanted_tags:
                score += 1.0 * len(wanted_tags & entry_tags)
            # Slight recency boost so newer notes win on ties.
            updated = summary.get("updated_at") or summary.get("created_at") or ""
            if updated:
                score += 0.01  # epsilon ensures stable sort on equal scores

            if score > 0 or (not q and not wanted_tags):
                scored.append((score, {**summary, "id": kid}))

        scored.sort(
            key=lambda x: (
                -x[0],
                # ISO timestamps sort lex correctly; reverse for newest first.
                # Sort by negative tuple-index requires inverted compare below.
            )
        )
        if not q and not wanted_tags:
            # Default browse = most recently updated first.
            scored.sort(key=lambda x: x[1].get("updated_at") or x[1].get("created_at") or "", reverse=True)
        else:
            scored.sort(
                key=lambda x: (x[0], x[1].get("updated_at") or x[1].get("created_at") or ""),
                reverse=True,
            )
        return [s for _, s in scored[:max(1, int(limit))]]

    def save(
        self,
        *,
        title: str,
        body: str,
        tags: Iterable[str] | None = None,
        trigger_terms: Iterable[str] | None = None,
        applies_to: Iterable[str] | None = None,
        confidence: str = "probable",
        related: Iterable[str] | None = None,
        task_id: str = "",
        session_id: str = "",
        supersedes: str = "",
        reason: str = "初次沉淀",
    ) -> dict[str, Any]:
        clean_title = (title or "").strip()[:200] or "(untitled)"
        clean_body = (body or "").strip() or "_(no content)_"
        kid = _new_id()
        # Make sure id is unique (1-in-millions birthday paradox, but worth checking).
        while self._entry_path(kid).exists():
            kid = _new_id()
        now = _now_iso()
        meta: dict[str, Any] = {
            "id": kid,
            "title": clean_title,
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "created_session_id": str(session_id or ""),
            "last_updated_session_id": str(session_id or ""),
            "task_id": str(task_id or ""),
            "tags": _slug_tags(tags or []),
            "trigger_terms": _str_list(trigger_terms or [], max_items=24, max_len=80),
            "applies_to": _str_list(applies_to or [], max_items=12, max_len=240),
            "confidence": _normalize_confidence(confidence),
            "supersedes": str(supersedes or ""),
            "superseded_by": "",
            "related": _str_list(related or [], max_items=24, max_len=120),
            "changelog": [
                {"v": 1, "at": now, "action": "created", "reason": str(reason or "初次沉淀")[:300]}
            ],
        }
        self._write_entry(meta, clean_body)
        self._index[kid] = self._summary_from_meta(meta, f"{kid}.md")

        # If this entry supersedes an older one, link them both ways.
        if supersedes:
            old_rec = self._read_entry(str(supersedes))
            if old_rec is not None:
                old_meta, old_body, old_path = old_rec
                old_meta["superseded_by"] = kid
                old_meta["updated_at"] = now
                old_meta.setdefault("changelog", []).append(
                    {"v": int(old_meta.get("version") or 1), "at": now, "action": "superseded", "reason": f"被 {kid} 替代"}
                )
                self._write_entry(old_meta, old_body, old_path)
                self._index[str(supersedes)] = self._summary_from_meta(old_meta, old_path.name)

        self._save_index()
        return self.get(kid) or {}

    def update(
        self,
        entry_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        tags: Iterable[str] | None = None,
        trigger_terms: Iterable[str] | None = None,
        applies_to: Iterable[str] | None = None,
        confidence: str | None = None,
        related: Iterable[str] | None = None,
        task_id: str | None = None,
        session_id: str = "",
        reason: str = "",
    ) -> dict[str, Any] | None:
        rec = self._read_entry(entry_id)
        if rec is None:
            return None
        meta, old_body, path = rec
        # Don't allow editing trashed entries through update — restore first.
        if path.parent.name == self.TRASH_DIR:
            return None

        new_body = old_body if body is None else (body.strip() or old_body)
        if title is not None:
            meta["title"] = (title or "").strip()[:200] or meta.get("title") or "(untitled)"
        if tags is not None:
            meta["tags"] = _slug_tags(tags)
        if trigger_terms is not None:
            meta["trigger_terms"] = _str_list(trigger_terms, max_items=24, max_len=80)
        if applies_to is not None:
            meta["applies_to"] = _str_list(applies_to, max_items=12, max_len=240)
        if confidence is not None:
            meta["confidence"] = _normalize_confidence(confidence)
        if related is not None:
            meta["related"] = _str_list(related, max_items=24, max_len=120)
        if task_id is not None:
            meta["task_id"] = str(task_id or "")

        now = _now_iso()
        new_version = int(meta.get("version") or 1) + 1
        meta["version"] = new_version
        meta["updated_at"] = now
        if session_id:
            meta["last_updated_session_id"] = str(session_id)
        meta.setdefault("changelog", []).append(
            {"v": new_version, "at": now, "action": "updated", "reason": str(reason or "")[:300]}
        )
        # Cap changelog so frontmatter doesn't grow unbounded.
        if len(meta["changelog"]) > 30:
            meta["changelog"] = meta["changelog"][-30:]

        self._write_entry(meta, new_body, path)
        self._index[entry_id] = self._summary_from_meta(meta, path.name)
        self._save_index()
        return self.get(entry_id)

    def supersede(self, old_id: str, new_id: str, *, reason: str = "") -> bool:
        old_rec = self._read_entry(old_id)
        new_rec = self._read_entry(new_id)
        if old_rec is None or new_rec is None:
            return False
        now = _now_iso()
        old_meta, old_body, old_path = old_rec
        new_meta, new_body, new_path = new_rec
        old_meta["superseded_by"] = new_id
        old_meta["updated_at"] = now
        old_meta.setdefault("changelog", []).append(
            {"v": int(old_meta.get("version") or 1), "at": now, "action": "superseded", "reason": str(reason or "")[:300] or f"被 {new_id} 替代"}
        )
        new_meta["supersedes"] = old_id
        new_meta["updated_at"] = now
        new_meta.setdefault("changelog", []).append(
            {"v": int(new_meta.get("version") or 1), "at": now, "action": "supersedes", "reason": str(reason or "")[:300] or f"替代 {old_id}"}
        )
        self._write_entry(old_meta, old_body, old_path)
        self._write_entry(new_meta, new_body, new_path)
        self._index[old_id] = self._summary_from_meta(old_meta, old_path.name)
        self._index[new_id] = self._summary_from_meta(new_meta, new_path.name)
        self._save_index()
        return True

    def delete(self, entry_id: str, *, reason: str = "") -> bool:
        rec = self._read_entry(entry_id)
        if rec is None:
            return False
        meta, body, path = rec
        if path.parent.name == self.TRASH_DIR:
            return True
        now = _now_iso()
        meta["updated_at"] = now
        meta.setdefault("changelog", []).append(
            {"v": int(meta.get("version") or 1), "at": now, "action": "deleted", "reason": str(reason or "")[:300]}
        )
        trash_path = self.root / self.TRASH_DIR / f"{entry_id}.md"
        self._write_entry(meta, body, trash_path)
        try:
            path.unlink()
        except OSError:
            pass
        self._index.pop(entry_id, None)
        self._save_index()
        return True

    def restore(self, entry_id: str) -> dict[str, Any] | None:
        trash_path = self.root / self.TRASH_DIR / f"{entry_id}.md"
        if not trash_path.exists():
            return None
        text = trash_path.read_text(encoding="utf-8")
        meta, body = _split_frontmatter(text)
        meta["id"] = entry_id
        now = _now_iso()
        meta["updated_at"] = now
        meta.setdefault("changelog", []).append(
            {"v": int(meta.get("version") or 1), "at": now, "action": "restored", "reason": ""}
        )
        new_path = self._entry_path(entry_id)
        self._write_entry(meta, body, new_path)
        try:
            trash_path.unlink()
        except OSError:
            pass
        self._index[entry_id] = self._summary_from_meta(meta, new_path.name)
        self._save_index()
        return self.get(entry_id)

    # -- trash maintenance (hard delete + purge) ---------------------------

    def list_trash(self) -> list[dict[str, Any]]:
        """Return summaries of every entry currently in ``_trash/``.

        Each summary carries a synthetic ``age_days`` (rounded down) and the
        ``size_bytes`` of the underlying ``.md`` so the UI / cleanup helpers
        can rank candidates without re-reading frontmatter twice.
        """
        trash_dir = self.root / self.TRASH_DIR
        if not trash_dir.exists():
            return []
        now = _time.time()
        out: list[dict[str, Any]] = []
        for md in trash_dir.glob("kb-*.md"):
            try:
                meta, _body = _split_frontmatter(md.read_text(encoding="utf-8"))
            except Exception:
                continue
            try:
                st = md.stat()
            except OSError:
                continue
            summary = self._summary_from_meta(meta, md.name)
            summary["trashed"] = True
            summary["id"] = meta.get("id") or md.stem
            summary["size_bytes"] = int(st.st_size)
            summary["age_days"] = max(0, int((now - st.st_mtime) // 86400))
            out.append(summary)
        out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
        return out

    def trash_size_bytes(self) -> int:
        """Total disk usage of ``_trash/`` (sum of ``.md`` sizes)."""
        trash_dir = self.root / self.TRASH_DIR
        if not trash_dir.exists():
            return 0
        total = 0
        for md in trash_dir.glob("kb-*.md"):
            try:
                total += md.stat().st_size
            except OSError:
                continue
        return total

    def hard_delete(self, entry_id: str) -> bool:
        """Permanently remove a trashed entry.

        ONLY removes files inside ``_trash/`` — never touches an active entry
        (you must ``delete`` first, which moves it). Returns True if the
        target file existed and was unlinked. Designed to be called from
        explicit user actions (UI button / settings purge), NOT from AI.
        """
        safe_id = str(entry_id or "").strip()
        if not safe_id:
            return False
        trash_path = self.root / self.TRASH_DIR / f"{safe_id}.md"
        if not trash_path.exists():
            return False
        try:
            trash_path.unlink()
        except OSError as e:
            log.warning("Failed to hard-delete %s: %s", trash_path, e)
            return False
        return True

    def purge_trash(self, older_than_days: int | None = None) -> dict[str, Any]:
        """Hard-delete trashed entries.

        - ``older_than_days=None`` or ``<=0``: wipe **all** trashed entries.
        - ``older_than_days=N``: only wipe entries whose file mtime is older
          than N days (so users get a "grace period" after deletion).

        Returns a dict ``{removed: [ids], freed_bytes: int, kept: int}``.
        """
        trash_dir = self.root / self.TRASH_DIR
        if not trash_dir.exists():
            return {"removed": [], "freed_bytes": 0, "kept": 0}
        threshold: float | None
        if older_than_days is None or int(older_than_days) <= 0:
            threshold = None
        else:
            threshold = _time.time() - int(older_than_days) * 86400.0
        removed: list[str] = []
        freed = 0
        kept = 0
        for md in trash_dir.glob("kb-*.md"):
            try:
                st = md.stat()
            except OSError:
                continue
            if threshold is not None and st.st_mtime > threshold:
                kept += 1
                continue
            size = int(st.st_size)
            try:
                md.unlink()
            except OSError as e:
                log.warning("Failed to purge %s: %s", md, e)
                continue
            removed.append(md.stem)
            freed += size
        return {"removed": removed, "freed_bytes": freed, "kept": kept}

    def suggest_cleanup(self, max_results: int = 50, min_age_days: int = 30) -> list[dict[str, Any]]:
        """Return trashed entries that look like reasonable cleanup candidates.

        Heuristic: entries that are at least ``min_age_days`` old, sorted
        oldest-first so the AI can build a "want to purge these?" prompt for
        the user. NEVER deletes anything — purely advisory.
        """
        candidates = [t for t in self.list_trash() if t.get("age_days", 0) >= int(min_age_days)]
        candidates.sort(key=lambda x: x.get("age_days") or 0, reverse=True)
        return candidates[: max(1, int(max_results))]
