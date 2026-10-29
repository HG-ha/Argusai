"""Normalized finding model shared by all checks and reports."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        order = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        return order[self.value]


@dataclass
class Finding:
    """A single security finding emitted by a check.

    ``id`` is a stable slug like ``"headers.missing-csp"`` used for dedup
    across scans. ``uid`` is a per-instance uuid for cross-referencing in the
    report / GUI.
    """

    id: str
    category: str
    severity: Severity
    title: str
    url: str | None = None
    description: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: str = ""
    cwe: str | None = None
    references: list[str] = field(default_factory=list)
    target: str = ""
    check: str = ""
    domain: str = "web"
    kind: str = "verification"
    confidence: str = "suspected"
    artifact_refs: list[str] = field(default_factory=list)
    interaction_refs: list[str] = field(default_factory=list)
    recommended_tools: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    )

    def fingerprint(self) -> str:
        """Content-hash for dedup across scans (same target + id + url)."""
        raw = f"{self.target}|{self.id}|{self.url or ''}".encode()
        return hashlib.sha1(raw).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Finding":
        d = dict(d)
        d["severity"] = Severity(d["severity"])
        return cls(**d)
