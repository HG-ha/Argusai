"""Assistant tool executors split from the former monolithic module."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

from .common import *

def _skills_root():
    from pathlib import Path

    # argusai/core/tools/skills.py → parent.parent.parent = argusai/
    return Path(__file__).resolve().parent.parent.parent / "skills"


def _parse_skill_doc(path):
    import re
    import yaml

    text = path.read_text(encoding="utf-8", errors="replace")
    meta: dict[str, Any] = {}
    body = text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if m:
        try:
            raw = yaml.safe_load(m.group(1)) or {}
            meta = raw if isinstance(raw, dict) else {}
        except Exception:
            meta = {}
        body = m.group(2)
    name = str(meta.get("name") or path.parent.name)
    description = str(meta.get("description") or "").strip()
    if not description:
        description = body.strip().splitlines()[0] if body.strip() else name
    return {
        "name": name,
        "description": description,
        "body": body.strip(),
        "path": str(path),
        "dir": str(path.parent),
        "user_invocable": meta.get("user-invocable", True) is not False,
        "disable_model_invocation": bool(meta.get("disable-model-invocation")),
    }


def _load_project_skills() -> list[dict[str, Any]]:
    root = _skills_root()
    out: list[dict[str, Any]] = []
    if not root.exists():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        path = child / "SKILL.md"
        if not path.exists():
            continue
        try:
            out.append(_parse_skill_doc(path))
        except Exception:
            continue
    return out


def skill_list(api: Any, include_disabled: bool = False, **kwargs: Any) -> dict[str, Any]:
    """List project skills available to ArgusAI's assistant."""
    skills = _load_project_skills()
    if not include_disabled:
        skills = [s for s in skills if s.get("user_invocable") and not s.get("disable_model_invocation")]
    summaries = [
        {
            "name": s["name"],
            "description": s["description"],
            "path": s["path"],
            "body_chars": len(s.get("body") or ""),
        }
        for s in skills
    ]
    return {"ok": True, "count": len(summaries), "skills": summaries}


def skill_search(
    api: Any,
    query: str = "",
    limit: int = 8,
    include_disabled: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Search project skills by name, description and body text."""
    terms = [t.lower() for t in str(query or "").split() if t.strip()]
    skills = _load_project_skills()
    if not include_disabled:
        skills = [s for s in skills if s.get("user_invocable") and not s.get("disable_model_invocation")]
    matches = []
    for skill in skills:
        hay_name = str(skill.get("name") or "").lower()
        hay_desc = str(skill.get("description") or "").lower()
        hay_body = str(skill.get("body") or "").lower()
        if not terms:
            score = 1
        else:
            score = 0
            for term in terms:
                if term in hay_name:
                    score += 8
                if term in hay_desc:
                    score += 4
                if term in hay_body:
                    score += 1
        if score <= 0:
            continue
        body = skill.get("body") or ""
        matches.append({
            "name": skill["name"],
            "description": skill["description"],
            "path": skill["path"],
            "score": score,
            "body_preview": body[:500],
            "body_chars": len(body),
        })
    matches.sort(key=lambda x: (-int(x["score"]), str(x["name"])))
    max_items = _safe_int(limit, 8, 1, 50)
    return {"ok": True, "query": query, "count": len(matches), "skills": matches[:max_items]}


def skill_read(
    api: Any,
    name: str = "",
    max_chars: int = 24000,
    offset: int = 0,
    line_offset: int = 0,
    line_limit: int = 0,
    heading: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Read one project skill body by name, with char, line, or heading slicing."""
    needle = str(name or "").strip().lower()
    if not needle:
        return {"ok": False, "error": "name is required"}
    for skill in _load_project_skills():
        dir_name = str(skill.get("dir") or "").replace("\\", "/").rstrip("/").split("/")[-1].lower()
        if needle not in {str(skill.get("name") or "").lower(), dir_name}:
            continue
        body = str(skill.get("body") or "")
        total_chars = len(body)
        lines = body.splitlines(keepends=True)
        total_lines = len(lines)

        selected_heading = ""
        line_start: int | None = None
        line_end: int | None = None
        char_start = max(0, int(offset or 0))
        char_end = min(total_chars, char_start + _safe_int(max_chars, 24000, 256, 100000))

        if str(heading or "").strip():
            selected_heading = str(heading or "").strip()
            found = _skill_heading_slice(lines, selected_heading)
            if not found:
                return {
                    "ok": False,
                    "error": "heading not found",
                    "name": skill["name"],
                    "heading": selected_heading,
                    "headings": _skill_headings(lines),
                }
            line_start, line_end = found
            char_start = sum(len(line) for line in lines[:line_start])
            char_end = sum(len(line) for line in lines[:line_end])
        elif int(line_limit or 0) > 0:
            line_start = max(0, int(line_offset or 0))
            line_count = _safe_int(line_limit, 120, 1, 2000)
            line_end = min(total_lines, line_start + line_count)
            char_start = sum(len(line) for line in lines[:line_start])
            char_end = sum(len(line) for line in lines[:line_end])

        out_body = body[char_start:char_end]
        return {
            "ok": True,
            "name": skill["name"],
            "description": skill["description"],
            "path": skill["path"],
            "body": out_body,
            "offset": char_start,
            "end_offset": char_end,
            "total_chars": total_chars,
            "line_start": line_start,
            "line_end": line_end,
            "total_lines": total_lines,
            "heading": selected_heading,
            "truncated": char_end < total_chars,
            "more": char_end < total_chars,
        }
    return {"ok": False, "error": "skill not found", "name": name}


def _skill_heading_level(line: str) -> int:
    stripped = line.lstrip()
    if not stripped.startswith("#"):
        return 0
    hashes = len(stripped) - len(stripped.lstrip("#"))
    if hashes > 6 or len(stripped) <= hashes or stripped[hashes] != " ":
        return 0
    return hashes


def _skill_heading_text(line: str) -> str:
    level = _skill_heading_level(line)
    if not level:
        return ""
    return line.lstrip()[level:].strip().strip("#").strip()


def _skill_heading_slice(lines: list[str], heading: str) -> tuple[int, int] | None:
    needle = heading.strip().lower()
    for idx, line in enumerate(lines):
        level = _skill_heading_level(line)
        if not level:
            continue
        text = _skill_heading_text(line)
        if text.lower() != needle:
            continue
        end = len(lines)
        for j in range(idx + 1, len(lines)):
            next_level = _skill_heading_level(lines[j])
            if next_level and next_level <= level:
                end = j
                break
        return idx, end
    return None


def _skill_headings(lines: list[str]) -> list[dict[str, Any]]:
    out = []
    for idx, line in enumerate(lines):
        level = _skill_heading_level(line)
        if level:
            out.append({"line": idx, "level": level, "title": _skill_heading_text(line)})
    return out[:200]
