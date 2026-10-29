"""Tests for artifact preview helpers (webui reverseUtils parity)."""

from __future__ import annotations

import json


def _format_artifact_preview(kind: str, raw: str) -> str:
    """Mirror of webui/src/components/reverse/reverseUtils.js formatArtifactPreview."""
    text = str(raw or "")
    k = str(kind or "")
    if not text.strip():
        return text
    if k in ("capa_report", "yara_match"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if k == "capa_report":
            rules = list((data.get("rules") or {}).keys()) if isinstance(data.get("rules"), dict) else []
            meta = data.get("meta") or {}
            header_lines = ["# capa report"]
            if meta.get("format"):
                header_lines.append(f"format: {meta['format']}")
            if rules:
                preview = ", ".join(rules[:12])
                suffix = "…" if len(rules) > 12 else ""
                header_lines.append(f"rules ({len(rules)}): {preview}{suffix}")
            header = "\n".join(header_lines)
            return f"{header}\n\n{json.dumps(data, indent=2, ensure_ascii=False)}"
        matches = data.get("matches") if isinstance(data.get("matches"), list) else []
        count = data.get("match_count", len(matches))
        header = f"# yara matches ({count})\n" + "\n".join(matches[:20])
        return f"{header}\n\n{json.dumps(data, indent=2, ensure_ascii=False)}"
    return text


def test_format_capa_preview_includes_rule_names() -> None:
    payload = json.dumps({"meta": {"format": "pe"}, "rules": {"rule.a": {}, "rule.b": {}}})
    out = _format_artifact_preview("capa_report", payload)
    assert "# capa report" in out
    assert "rule.a" in out
    assert "rules (2)" in out


def test_format_yara_preview_includes_match_lines() -> None:
    payload = json.dumps({"match_count": 1, "matches": ["evil sample.exe"]})
    out = _format_artifact_preview("yara_match", payload)
    assert "# yara matches (1)" in out
    assert "evil sample.exe" in out
