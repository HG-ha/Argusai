"""Case, target, artifact and evidence storage for AI-driven analysis.

This module is intentionally domain-neutral. Web requests, browser traces,
pcaps, APKs, EXEs and decompiler output are all represented as artifacts or
interactions under a case timeline so the assistant can keep stable references
even after old tool results are compacted out of chat context.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import string
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


CASE_ID_RE = re.compile(r"^case-\d{8}-[A-Za-z0-9]{4,8}$")
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
CASE_TARGET_TYPES = {
    "url", "host", "cidr", "service", "apk", "ipa", "exe", "dll", "elf",
    "process", "dump", "pcap", "repository", "device", "package",
    "cluster", "namespace", "cloud_account", "iac_repo", "container_image",
    "ad_domain", "dc", "sbom", "wifi_interface", "wifi_pcap",
    "firmware", "rootfs", "board", "uart_capture", "flash_dump", "other",
}
ARTIFACT_KINDS = {
    "request", "response", "interaction", "trace", "har", "pcap", "binary",
    "apk", "ipa", "exe", "dll", "elf", "source", "decompiled", "screenshot",
    "log", "finding", "oracle", "oob", "note", "ai_prompt", "ai_response",
    "tool_call_log", "network", "config", "manifest", "policy", "sbom",
    "lockfile", "vuln_report", "wireless", "firmware", "filesystem",
    "ldap", "kerberos", "graph", "uart", "flash", "photo", "other",
}
EVIDENCE_KINDS = {
    "finding", "http", "oob", "oracle", "diff", "trace", "screenshot",
    "static_analysis", "dynamic_analysis", "network", "mobile", "binary",
    "ai", "dom", "tool_call", "source", "cloud", "config", "directory",
    "kerberos", "sca", "dependency", "wireless", "firmware", "hardware",
    "note", "other",
}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _new_id(prefix: str) -> str:
    return f"{prefix}-{''.join(random.choices(string.ascii_letters + string.digits, k=6))}"


def _new_case_id() -> str:
    return f"case-{time.strftime('%Y%m%d')}-{''.join(random.choices(string.ascii_letters + string.digits, k=4))}"


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _norm(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _atomic_write(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


@dataclass
class CaseTarget:
    type: str
    value: str
    name: str = ""
    scope: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["type"] = _norm(data.get("type"), CASE_TARGET_TYPES, "other")
        return data


@dataclass
class ArtifactRef:
    id: str
    kind: str
    path: str
    title: str = ""
    domain: str = ""
    media_type: str = ""
    sha256: str = ""
    size: int = 0
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = _norm(data.get("kind"), ARTIFACT_KINDS, "other")
        return data


@dataclass
class EvidenceRef:
    id: str
    kind: str
    title: str = ""
    confidence: str = "suspected"
    artifact_refs: list[str] = field(default_factory=list)
    interaction_refs: list[str] = field(default_factory=list)
    finding_uid: str = ""
    summary: str = ""
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = _norm(data.get("kind"), EVIDENCE_KINDS, "other")
        return data


class CaseStore:
    """File-backed case store under ``<data_root>/cases``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def _case_dir(self, case_id: str) -> Path:
        cid = str(case_id or "").strip()
        if not CASE_ID_RE.match(cid):
            raise FileNotFoundError(f"invalid case_id: {case_id}")
        return self.root / cid

    def _load(self, case_id: str) -> tuple[Path, dict[str, Any]]:
        case_dir = self._case_dir(case_id)
        path = case_dir / "case.json"
        if not path.exists():
            raise FileNotFoundError(f"case not found: {case_id}")
        return case_dir, json.loads(path.read_text(encoding="utf-8"))

    def _write(self, case_dir: Path, data: dict[str, Any]) -> None:
        data["updated_at"] = now_iso()
        _atomic_write(case_dir / "case.json", data)

    def _event(self, case_dir: Path, kind: str, data: dict[str, Any]) -> None:
        _append_jsonl(case_dir / "events.jsonl", {"ts": now_iso(), "kind": kind, **data})

    def create_case(
        self,
        *,
        title: str = "",
        target: CaseTarget | dict[str, Any] | None = None,
        domain: str = "",
        tags: list[Any] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        case_id = _new_case_id()
        case_dir = self.root / case_id
        (case_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        tgt = target if isinstance(target, CaseTarget) else CaseTarget(**(target or {"type": "other", "value": ""}))
        data = {
            "id": case_id,
            "title": str(title or "").strip() or case_id,
            "domain": str(domain or "").strip().lower(),
            "target": tgt.to_dict(),
            "tags": [str(t).strip() for t in (tags or []) if str(t).strip()][:32],
            "note": str(note or "")[:2000],
            "artifacts": [],
            "evidence": [],
            "links": [],
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        self._write(case_dir, data)
        self._event(case_dir, "created", {"case_id": case_id})
        return data

    def get_case(self, case_id: str) -> dict[str, Any]:
        _case_dir, data = self._load(case_id)
        return data

    def list_cases(self, *, limit: int = 50) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for child in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not child.is_dir() or not CASE_ID_RE.match(child.name):
                continue
            path = child / "case.json"
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            items.append({
                "id": data.get("id"),
                "title": data.get("title"),
                "domain": data.get("domain"),
                "target": data.get("target"),
                "tags": data.get("tags") or [],
                "artifact_count": len(data.get("artifacts") or []),
                "evidence_count": len(data.get("evidence") or []),
                "updated_at": data.get("updated_at"),
            })
            if len(items) >= limit:
                break
        return items

    def add_artifact(
        self,
        case_id: str,
        *,
        kind: str = "other",
        title: str = "",
        content: str | bytes = b"",
        filename: str = "",
        domain: str = "",
        media_type: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        case_dir, data = self._load(case_id)
        artifact_id = _new_id("art")
        safe_name = filename if SAFE_FILENAME_RE.match(filename or "") else f"{artifact_id}.txt"
        rel = f"artifacts/{artifact_id}-{safe_name}"
        path = case_dir / rel
        raw = content if isinstance(content, bytes) else str(content or "").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        ref = ArtifactRef(
            id=artifact_id,
            kind=kind,
            title=title,
            path=rel,
            domain=domain,
            media_type=media_type,
            sha256=_sha256_bytes(raw),
            size=len(raw),
            metadata=dict(metadata or {}),
        ).to_dict()
        data.setdefault("artifacts", []).append(ref)
        self._write(case_dir, data)
        self._event(case_dir, "artifact_added", {"id": artifact_id, "kind": ref["kind"], "path": rel})
        return ref

    def read_artifact(self, case_id: str, artifact_id: str, *, max_chars: int = 32768, offset: int = 0) -> dict[str, Any]:
        case_dir, data = self._load(case_id)
        artifact = next((a for a in data.get("artifacts") or [] if a.get("id") == artifact_id), None)
        if artifact is None:
            raise FileNotFoundError(f"artifact not found: {artifact_id}")
        path = (case_dir / str(artifact.get("path") or "")).resolve()
        if not str(path).startswith(str(case_dir.resolve())):
            raise FileNotFoundError("artifact path escapes case directory")
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        start = max(0, int(offset or 0))
        end = min(len(text), start + max(0, int(max_chars or 0)))
        return {
            "artifact": artifact,
            "content": text[start:end],
            "offset": start,
            "total_chars": len(text),
            "truncated": end < len(text),
        }

    def add_evidence(
        self,
        case_id: str,
        *,
        kind: str = "other",
        title: str = "",
        confidence: str = "suspected",
        artifact_refs: list[Any] | None = None,
        interaction_refs: list[Any] | None = None,
        finding_uid: str = "",
        summary: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        case_dir, data = self._load(case_id)
        ref = EvidenceRef(
            id=_new_id("ev"),
            kind=kind,
            title=title,
            confidence=confidence,
            artifact_refs=[str(x) for x in (artifact_refs or []) if str(x)],
            interaction_refs=[str(x) for x in (interaction_refs or []) if str(x)],
            finding_uid=str(finding_uid or ""),
            summary=str(summary or "")[:4000],
            metadata=dict(metadata or {}),
        ).to_dict()
        data.setdefault("evidence", []).append(ref)
        self._write(case_dir, data)
        self._event(case_dir, "evidence_added", {"id": ref["id"], "kind": ref["kind"]})
        return ref

    def add_link(
        self,
        case_id: str,
        *,
        source_ref: str,
        relation: str,
        target_ref: str,
        note: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add a lightweight graph edge between case resources."""
        case_dir, data = self._load(case_id)
        src = str(source_ref or "").strip()[:240]
        dst = str(target_ref or "").strip()[:240]
        rel = str(relation or "").strip().lower()[:80]
        if not src or not dst or not rel:
            raise ValueError("source_ref, relation and target_ref are required")
        link = {
            "id": _new_id("link"),
            "source_ref": src,
            "relation": rel,
            "target_ref": dst,
            "note": str(note or "")[:1000],
            "metadata": dict(metadata or {}),
            "created_at": now_iso(),
        }
        data.setdefault("links", []).append(link)
        self._write(case_dir, data)
        self._event(case_dir, "link_added", {"id": link["id"], "source_ref": src, "relation": rel, "target_ref": dst})
        return link

    def graph(self, case_id: str) -> dict[str, Any]:
        """Build a small evidence graph from artifacts, evidence and links."""
        _case_dir, data = self._load(case_id)
        nodes: dict[str, dict[str, Any]] = {}

        def add_node(ref: str, kind: str, label: str = "", data_obj: dict[str, Any] | None = None) -> None:
            key = str(ref or "").strip()
            if not key:
                return
            nodes[key] = {
                "id": key,
                "kind": kind,
                "label": label or key,
                "data": data_obj or {},
            }

        derived_edges: list[dict[str, Any]] = []
        for artifact in data.get("artifacts") or []:
            add_node(f"artifact:{artifact.get('id')}", "artifact", artifact.get("title") or artifact.get("id"), artifact)
        for evidence in data.get("evidence") or []:
            ev_ref = f"evidence:{evidence.get('id')}"
            add_node(ev_ref, "evidence", evidence.get("title") or evidence.get("id"), evidence)
            for artifact_ref in evidence.get("artifact_refs") or []:
                art_ref = f"artifact:{artifact_ref}"
                add_node(art_ref, "artifact", str(artifact_ref))
                derived_edges.append({
                    "id": f"derived-{evidence.get('id')}-{artifact_ref}",
                    "source_ref": ev_ref,
                    "relation": "supported_by",
                    "target_ref": art_ref,
                    "note": "derived from evidence.artifact_refs",
                    "derived": True,
                })
            if evidence.get("finding_uid"):
                finding_ref = f"finding:{evidence.get('finding_uid')}"
                add_node(finding_ref, "finding", str(evidence.get("finding_uid")))
                derived_edges.append({
                    "id": f"derived-{evidence.get('id')}-{evidence.get('finding_uid')}",
                    "source_ref": finding_ref,
                    "relation": "validated_by",
                    "target_ref": ev_ref,
                    "note": "derived from evidence.finding_uid",
                    "derived": True,
                })
        edges = [*list(data.get("links") or []), *derived_edges]
        for edge in edges:
            add_node(edge.get("source_ref"), "ref")
            add_node(edge.get("target_ref"), "ref")
        return {
            "case_id": data.get("id") or case_id,
            "nodes": list(nodes.values()),
            "edges": edges,
        }

    def events(self, case_id: str, *, tail: int = 200) -> list[dict[str, Any]]:
        case_dir, _data = self._load(case_id)
        path = case_dir / "events.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        if tail > 0:
            lines = lines[-tail:]
        out: list[dict[str, Any]] = []
        for line in lines:
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    out.append(obj)
            except Exception:
                continue
        return out
