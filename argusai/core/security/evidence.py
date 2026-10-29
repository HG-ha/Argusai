"""Structured evidence and reproducible test case helpers."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class EvidenceRecord:
    """A compact, report-friendly record for proof and reproduction."""

    kind: str
    surface: str = ""
    payload: str = ""
    url: str = ""
    method: str = "GET"
    status_code: int | None = None
    request: dict[str, Any] = field(default_factory=dict)
    response_excerpt: str = ""
    browser_signal: str = ""
    confidence: str = "suspected"
    repro_steps: list[str] = field(default_factory=list)
    test_case: str = ""
    notes: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in ("", None, [], {})}


def evidence_record(**kwargs: Any) -> dict[str, Any]:
    return EvidenceRecord(**kwargs).to_dict()


def merge_evidence(existing: dict[str, Any] | None, record: dict[str, Any]) -> dict[str, Any]:
    out = dict(existing or {})
    records = list(out.get("records") or [])
    records.append(record)
    out["records"] = records
    if record.get("payload"):
        out.setdefault("payload", record.get("payload"))
    if record.get("test_case"):
        out.setdefault("test_case", record.get("test_case"))
    if record.get("confidence"):
        out.setdefault("confidence", record.get("confidence"))
    return out


def curl_repro(url: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: str = "") -> str:
    parts = ["curl", "-i", "-X", method.upper()]
    for k, v in (headers or {}).items():
        parts += ["-H", json.dumps(f"{k}: {v}")]
    if body:
        parts += ["--data-binary", json.dumps(body)]
    parts.append(json.dumps(url))
    return " ".join(parts)


def response_excerpt(text: str, needle: str = "", limit: int = 800) -> str:
    if not text:
        return ""
    if needle and needle in text:
        idx = text.find(needle)
        start = max(0, idx - limit // 2)
        end = min(len(text), idx + len(needle) + limit // 2)
        return text[start:end].replace("\n", " ")
    return text[:limit].replace("\n", " ")

