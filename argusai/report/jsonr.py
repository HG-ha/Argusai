"""JSON report writer."""

from __future__ import annotations

import json
from pathlib import Path

from argusai.core.runner import ScanResult


def render_json(result: ScanResult, out: Path) -> Path:
    payload = {
        "target": {
            "url": result.target.url,
            "name": result.target.name,
            "scope": result.target.scope,
        },
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "severity": result.by_severity(),
        "total": len(result.findings),
        "findings": [f.to_dict() for f in result.findings],
        "errors": result.errors,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out
