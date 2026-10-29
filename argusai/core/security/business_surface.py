"""Evidence-first business-surface extraction for Agent planning.

The helpers in this module do not decide the target's business model. They
turn browser/proxy/HAR/JS evidence into candidate signals with source ids so
the main Agent can reason, collect more evidence, and create concrete todos.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class BusinessEvidence:
    id: str
    kind: str
    source: str = ""
    url: str = ""
    method: str = "GET"
    text_excerpt: str = ""
    body_keys: list[str] = field(default_factory=list)
    artifact_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BusinessCandidate:
    name: str
    label: str
    category: str
    confidence: float
    signals: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    needs_ai_review: bool = True
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BusinessObservation:
    id: str
    evidence_id: str
    url: str = ""
    method: str = "GET"
    source: str = ""
    asset_candidates: list[BusinessCandidate] = field(default_factory=list)
    operation_candidates: list[BusinessCandidate] = field(default_factory=list)
    risk_candidates: list[BusinessCandidate] = field(default_factory=list)
    unknown_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["asset_candidates"] = [c.to_dict() for c in self.asset_candidates]
        data["operation_candidates"] = [c.to_dict() for c in self.operation_candidates]
        data["risk_candidates"] = [c.to_dict() for c in self.risk_candidates]
        return data


ASSET_HINTS: dict[str, dict[str, Any]] = {
    "account": {"label": "account and identity", "keywords": ["account", "user", "profile", "member", "login", "logout", "password", "session"]},
    "tenant": {"label": "tenant or organization", "keywords": ["tenant", "org", "organization", "workspace", "team", "company"]},
    "role_admin": {"label": "role and admin", "keywords": ["admin", "role", "permission", "privilege", "rbac", "manager"]},
    "order": {"label": "order", "keywords": ["order", "trade", "booking", "reservation"]},
    "payment": {"label": "payment and money", "keywords": ["pay", "payment", "checkout", "charge", "balance", "wallet", "withdraw", "refund", "invoice"]},
    "coupon": {"label": "coupon and discount", "keywords": ["coupon", "promo", "promotion", "discount", "voucher"]},
    "points": {"label": "points and rewards", "keywords": ["point", "credit", "score", "level", "reward"]},
    "cart_product": {"label": "product, cart and inventory", "keywords": ["product", "sku", "goods", "cart", "stock", "inventory", "price"]},
    "file_upload": {"label": "file upload and download", "keywords": ["upload", "file", "attachment", "avatar", "image", "download", "media"]},
    "export_report": {"label": "export and report", "keywords": ["export", "report", "download", "csv", "excel", "xlsx", "pdf"]},
    "approval": {"label": "approval workflow", "keywords": ["approve", "approval", "reject", "workflow", "process", "state", "status"]},
    "message": {"label": "message and notification", "keywords": ["message", "notice", "notification", "mail", "sms", "inbox"]},
    "ticket": {"label": "ticket and support", "keywords": ["ticket", "support", "case", "issue"]},
    "callback": {"label": "third-party callback", "keywords": ["callback", "webhook", "notify", "oauth", "redirect", "return_url"]},
}


OPERATION_HINTS: dict[str, list[str]] = {
    "create": ["create", "add", "new", "submit", "register", "apply"],
    "read": ["get", "list", "detail", "view", "query", "search", "info"],
    "update": ["update", "edit", "modify", "change", "set", "save"],
    "delete": ["delete", "remove", "cancel", "close"],
    "pay": ["pay", "checkout", "charge"],
    "refund": ["refund", "return"],
    "approve": ["approve", "reject", "review"],
    "export": ["export", "download", "report"],
    "upload": ["upload", "import"],
    "invite": ["invite", "share"],
    "callback": ["callback", "webhook", "notify", "return"],
}


RISK_HINTS: dict[str, list[str]] = {
    "object_authorization": ["id", "uid", "user_id", "order_id", "tenant_id", "file_id", "invoice_id"],
    "tenant_boundary": ["tenant", "org", "workspace", "company"],
    "state_change": ["POST", "PUT", "PATCH", "DELETE", "approve", "pay", "refund", "submit", "delete"],
    "amount_or_quantity": ["amount", "price", "total", "money", "quantity", "stock"],
    "file_handling": ["upload", "download", "file", "attachment", "avatar", "media"],
    "third_party_callback": ["callback", "webhook", "notify", "return_url", "redirect"],
    "bulk_export": ["export", "report", "csv", "excel", "xlsx", "pdf"],
}


DISCOVERY_REQUIREMENTS: list[dict[str, Any]] = [
    {
        "id": "generic_baseline",
        "title": "Generic vulnerability baseline",
        "purpose": "Business candidates are hints; baseline SQLi/XSS/SSRF/CORS/CSRF/upload/auth/protocol coverage remains mandatory.",
        "evidence_needed": ["scan findings", "representative requests", "response diffs", "browser proof where applicable"],
    },
    {
        "id": "business_specific",
        "title": "Business-specific checks authored by the Agent",
        "purpose": "Use candidates to decide payment/order/tenant/admin/upload/export/approval/webhook style business tests.",
        "evidence_needed": ["candidate assets", "state-changing requests", "role/session labels", "observed business invariant"],
    },
    {
        "id": "system_network",
        "title": "System and network coverage",
        "purpose": "Use network/service/TLS/version evidence to decide system/network todos inside scope.",
        "evidence_needed": ["scope", "host/service profile", "TLS/service exposure", "version or debug endpoint evidence"],
    },
]


def classify_endpoint(url: str, method: str = "GET", body: Any = None, text: str = "") -> dict[str, Any]:
    """Return evidence-backed business candidates for one endpoint."""
    evidence = _evidence_from_parts("evidence-0", url=url, method=method, body=body, text=text)
    assets = _asset_candidates(evidence)
    operations = _operation_candidates(evidence)
    risks = _risk_candidates(evidence, operations)
    best_asset = assets[0] if assets else None
    best_operation = operations[0] if operations else None
    return {
        "asset": best_asset.name if best_asset else "",
        "asset_label": best_asset.label if best_asset else "",
        "operation": best_operation.name if best_operation else "",
        "risks": [c.name for c in risks],
        "tags": _candidate_tags(assets, operations, risks),
        "confidence": max([c.confidence for c in assets + operations] or [0.0]),
        "signals": sorted({s for c in assets + operations + risks for s in c.signals}),
        "business_candidates": {
            "assets": [c.to_dict() for c in assets],
            "operations": [c.to_dict() for c in operations],
            "risks": [c.to_dict() for c in risks],
        },
        "ai_review_required": True,
        "review_note": "These are hints for the main Agent, not confirmed business facts.",
    }


def business_surface_extract(items_json: str = "", html: str = "", text: str = "", source: str = "") -> dict[str, Any]:
    """Extract candidate business signals from collected evidence."""
    raw_items = _coerce_items(items_json)
    raw_items.extend(_extract_text_items(html, source or "html"))
    raw_items.extend(_extract_text_items(text, source or "text"))

    evidence_items: list[BusinessEvidence] = []
    observations: list[BusinessObservation] = []
    for idx, item in enumerate(raw_items[:1000]):
        endpoint = _normalise_item(item, source=source)
        if not endpoint.get("url") and not endpoint.get("text"):
            continue
        evidence = _evidence_from_parts(
            f"ev-{idx + 1}",
            url=endpoint.get("url", ""),
            method=endpoint.get("method", "GET"),
            body=endpoint.get("body"),
            text=endpoint.get("text", ""),
            source=endpoint.get("source", source or ""),
            kind=endpoint.get("kind", ""),
            artifact_ref=endpoint.get("artifact_ref", ""),
        )
        evidence_items.append(evidence)
        assets = _asset_candidates(evidence)
        operations = _operation_candidates(evidence)
        risks = _risk_candidates(evidence, operations)
        observations.append(BusinessObservation(
            id=f"obs-{idx + 1}",
            evidence_id=evidence.id,
            url=evidence.url,
            method=evidence.method,
            source=evidence.source,
            asset_candidates=assets,
            operation_candidates=operations,
            risk_candidates=risks,
            unknown_reason="" if assets else "No strong business-asset signal; inspect UI text, navigation, and authenticated traffic.",
        ))

    surface = {
        "observations": [obs.to_dict() for obs in observations],
        "evidence_refs": [ev.to_dict() for ev in evidence_items],
        "candidate_assets": [c.to_dict() for c in _merge_candidates([c for obs in observations for c in obs.asset_candidates])],
        "candidate_operations": [c.to_dict() for c in _merge_candidates([c for obs in observations for c in obs.operation_candidates])],
        "candidate_risks": [c.to_dict() for c in _merge_candidates([c for obs in observations for c in obs.risk_candidates])],
        "unknowns": _unknowns(evidence_items, observations),
        "suggested_next_discovery": _suggested_next_discovery(evidence_items, observations),
        "coverage_requirements": DISCOVERY_REQUIREMENTS,
        "source_summary": _source_summary(evidence_items, observations),
        "agent_guidance": [
            "Review candidates against UI, authenticated browser state, proxy traffic, and JS-discovered routes.",
            "Create concrete todos with set_todos/add_todos; do not copy candidates as confirmed findings.",
            "Keep generic baseline vulnerability coverage even when business candidates are strong.",
        ],
    }
    return {"ok": True, "surface": surface, **surface}


def business_test_plan(surface_json: str = "{}", depth: str = "standard", roles_json: str = "{}") -> dict[str, Any]:
    """Return coverage requirements and evidence gaps, not fixed todos."""
    surface = _load_surface(surface_json)
    roles = _load_json(roles_json, {})
    gaps = list(surface.get("unknowns") or [])
    if not roles:
        gaps.append({
            "id": "gap.roles",
            "reason": "No role/session metadata supplied; access-control and tenant-boundary todos need Agent confirmation.",
            "suggested_tools": ["session_vault_list", "auth_session_inventory", "auth_matrix_from_vault"],
        })
    return {
        "ok": True,
        "depth": (depth or "standard").lower(),
        "planner_role": "coverage_requirements_only",
        "coverage_principle": "The main Agent must generate concrete todos from evidence; this tool does not emit fixed scan templates.",
        "coverage_requirements": DISCOVERY_REQUIREMENTS,
        "candidate_summary": {
            "evidence_count": len(surface.get("evidence_refs") or []),
            "observation_count": len(surface.get("observations") or []),
            "asset_count": len(surface.get("candidate_assets") or []),
            "operation_count": len(surface.get("candidate_operations") or []),
            "risk_count": len(surface.get("candidate_risks") or []),
        },
        "evidence_gaps": gaps,
        "suggested_agent_actions": [
            "Use set_todos/add_todos to create generic baseline, business-specific, and system/network layers.",
            "For each business todo, include the evidence_id or artifact_ref that triggered it.",
            "If evidence is thin, add discovery todos before validation todos.",
        ],
    }


def business_risk_select(surface_json: str = "{}", asset: str = "", risk_type: str = "") -> dict[str, Any]:
    """Filter candidate business signals for Agent review."""
    surface = _load_surface(surface_json)
    assets = _filter_candidates(surface.get("candidate_assets") or [], (asset or "").strip().lower())
    risks = _filter_candidates(surface.get("candidate_risks") or [], (risk_type or "").strip().lower())
    related_ids = sorted({
        ev
        for item in assets + risks
        if isinstance(item, dict)
        for ev in item.get("evidence_ids") or []
    })
    evidence = [
        ev for ev in surface.get("evidence_refs") or []
        if isinstance(ev, dict) and ev.get("id") in related_ids
    ]
    return {
        "ok": True,
        "asset_filter": asset,
        "risk_filter": risk_type,
        "candidate_assets": assets,
        "candidate_risks": risks,
        "evidence_refs": evidence,
        "agent_guidance": "Use these candidates as review targets; confirm with browser/proxy/API evidence before validation todos.",
    }


def _asset_candidates(evidence: BusinessEvidence) -> list[BusinessCandidate]:
    haystack = _haystack(evidence)
    candidates = []
    for name, rule in ASSET_HINTS.items():
        hits = _keyword_hits(haystack, rule["keywords"])
        if not hits:
            continue
        candidates.append(BusinessCandidate(
            name=name,
            label=str(rule["label"]),
            category="asset",
            confidence=round(min(0.85, 0.25 + len(hits) * 0.12 + _source_bonus(evidence.source)), 3),
            signals=hits,
            evidence_ids=[evidence.id],
            rationale="Keyword/path/body signal suggests this business asset; the Agent must confirm semantics.",
        ))
    return sorted(candidates, key=lambda c: (-c.confidence, c.name))[:5]


def _operation_candidates(evidence: BusinessEvidence) -> list[BusinessCandidate]:
    haystack = _haystack(evidence)
    candidates = []
    for name, keywords in OPERATION_HINTS.items():
        hits = _keyword_hits(haystack, keywords)
        if hits:
            candidates.append(BusinessCandidate(
                name=name,
                label=name,
                category="operation",
                confidence=round(min(0.8, 0.25 + len(hits) * 0.12 + _source_bonus(evidence.source)), 3),
                signals=hits,
                evidence_ids=[evidence.id],
                rationale="Endpoint naming or UI text suggests this operation; the Agent must confirm side effects.",
            ))
    if not candidates and evidence.method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        fallback = {"GET": "read", "POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete"}[evidence.method.upper()]
        candidates.append(BusinessCandidate(
            name=fallback,
            label=fallback,
            category="operation",
            confidence=0.2,
            signals=[f"method:{evidence.method.lower()}"],
            evidence_ids=[evidence.id],
            rationale="HTTP method is only a weak operation hint.",
        ))
    return sorted(candidates, key=lambda c: (-c.confidence, c.name))[:5]


def _risk_candidates(evidence: BusinessEvidence, operations: list[BusinessCandidate]) -> list[BusinessCandidate]:
    haystack = _haystack(evidence)
    method = evidence.method.upper()
    candidates = []
    for name, keywords in RISK_HINTS.items():
        hits = _keyword_hits(haystack, keywords)
        if name == "state_change" and method in {"POST", "PUT", "PATCH", "DELETE"}:
            hits = sorted(set(hits + [f"method:{method.lower()}"]))
        if hits:
            candidates.append(BusinessCandidate(
                name=name,
                label=name,
                category="risk",
                confidence=round(min(0.78, 0.22 + len(hits) * 0.1 + _source_bonus(evidence.source)), 3),
                signals=hits[:8],
                evidence_ids=[evidence.id],
                rationale="Risk hypothesis for Agent planning, not a vulnerability finding.",
            ))
    if any(op.name in {"pay", "refund", "approve", "delete", "update"} for op in operations):
        candidates.append(BusinessCandidate(
            name="state_integrity",
            label="state_integrity",
            category="risk",
            confidence=0.45,
            signals=[f"operation:{op.name}" for op in operations[:3]],
            evidence_ids=[evidence.id],
            rationale="State-changing operations often need replay, ordering, role, and invariant checks.",
        ))
    return sorted(_dedupe_candidates(candidates), key=lambda c: (-c.confidence, c.name))[:8]


def _evidence_from_parts(
    evidence_id: str,
    *,
    url: str = "",
    method: str = "GET",
    body: Any = None,
    text: str = "",
    source: str = "",
    kind: str = "",
    artifact_ref: str = "",
) -> BusinessEvidence:
    return BusinessEvidence(
        id=evidence_id,
        kind=kind or ("endpoint" if url else "text"),
        source=source,
        url=str(url or ""),
        method=str(method or "GET").upper(),
        text_excerpt=_excerpt(" ".join([str(url or ""), str(text or ""), _preview_body(body)]), 500),
        body_keys=_body_keys(body),
        artifact_ref=str(artifact_ref or ""),
    )


def _merge_candidates(items: list[BusinessCandidate]) -> list[BusinessCandidate]:
    merged: dict[tuple[str, str], BusinessCandidate] = {}
    for item in items:
        key = (item.category, item.name)
        current = merged.get(key)
        if current is None:
            merged[key] = BusinessCandidate(
                name=item.name,
                label=item.label,
                category=item.category,
                confidence=item.confidence,
                signals=list(item.signals),
                evidence_ids=list(item.evidence_ids),
                rationale=item.rationale,
            )
            continue
        current.confidence = max(current.confidence, item.confidence)
        current.signals = sorted(set(current.signals + item.signals))[:12]
        current.evidence_ids = sorted(set(current.evidence_ids + item.evidence_ids))
    return sorted(merged.values(), key=lambda c: (-c.confidence, c.category, c.name))


def _unknowns(evidence_items: list[BusinessEvidence], observations: list[BusinessObservation]) -> list[dict[str, Any]]:
    if not evidence_items:
        return [{"id": "gap.no_evidence", "reason": "No browser/proxy/HAR/API/HTML evidence was supplied.", "suggested_tools": ["browser_trace", "get_proxy_traffic", "js_api_extract", "api_inventory_build"]}]
    out = []
    weak = [obs for obs in observations if not obs.asset_candidates]
    if weak:
        out.append({"id": "gap.business_semantics", "reason": f"{len(weak)} evidence items have no clear business asset signal.", "evidence_ids": [obs.evidence_id for obs in weak[:20]], "suggested_tools": ["browser_probe", "browser_trace", "extract_page_text", "js_api_extract"]})
    if not any(obs.risk_candidates for obs in observations):
        out.append({"id": "gap.risk_hypotheses", "reason": "No risk hypothesis could be inferred from the supplied evidence.", "suggested_tools": ["web_request_parse", "api_request_select", "browser_trace_form"]})
    return out


def _suggested_next_discovery(evidence_items: list[BusinessEvidence], observations: list[BusinessObservation]) -> list[dict[str, Any]]:
    sources = {ev.source.lower() for ev in evidence_items}
    suggestions = []
    if not any("browser" in src for src in sources):
        suggestions.append({"goal": "Collect real UI and network activity", "tools": ["browser_trace", "browser_probe", "extract_page_text"], "why": "Business semantics are stronger when tied to rendered UI, forms, and runtime requests."})
    if not any("proxy" in src or "har" in src for src in sources):
        suggestions.append({"goal": "Add authenticated traffic evidence", "tools": ["get_proxy_traffic", "har_import_analyze", "api_inventory_build"], "why": "Proxy/HAR traffic reveals logged-in APIs, state-changing requests, and role-specific behavior."})
    if not any("static_js" in src or "js" in src for src in sources):
        suggestions.append({"goal": "Mine hidden API routes from JavaScript", "tools": ["js_api_extract", "api_inventory_build", "business_surface_extract"], "why": "Static JS often contains routes that are not visible in one browser path."})
    if not any(obs.operation_candidates and obs.risk_candidates for obs in observations):
        suggestions.append({"goal": "Find state-changing business flows", "tools": ["browser_trace_form", "web_request_parse", "api_request_select"], "why": "Business validation needs concrete create/update/pay/approve/export requests before testing."})
    return suggestions


def _source_summary(evidence_items: list[BusinessEvidence], observations: list[BusinessObservation]) -> dict[str, Any]:
    by_source: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for ev in evidence_items:
        by_source[ev.source or "unknown"] = by_source.get(ev.source or "unknown", 0) + 1
        by_kind[ev.kind or "unknown"] = by_kind.get(ev.kind or "unknown", 0) + 1
    return {"input_items": len(evidence_items), "observations": len(observations), "by_source": dict(sorted(by_source.items())), "by_kind": dict(sorted(by_kind.items()))}


def _coerce_items(items_json: str = "") -> list[Any]:
    if not items_json:
        return []
    try:
        parsed = json.loads(items_json)
    except Exception:
        return [line.strip() for line in str(items_json).splitlines() if line.strip()]
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("endpoints", "requests", "entries", "flows", "traffic", "items"):
            val = parsed.get(key)
            if isinstance(val, list):
                return val
        return [parsed]
    return [parsed]


def _extract_text_items(value: str = "", source: str = "text") -> list[dict[str, Any]]:
    text = str(value or "")
    if not text.strip():
        return []
    out = []
    for m in re.finditer(r"https?://[^\s\"'<>]+|/[A-Za-z0-9_./?=&%-]+", text, re.I):
        out.append({"url": m.group(0), "method": "GET", "source": source, "text": text[max(0, m.start() - 160):m.end() + 160], "kind": "text_link"})
        if len(out) >= 200:
            break
    if not out:
        out.append({"text": text[:5000], "source": source, "kind": "text"})
    return out


def _normalise_item(item: Any, source: str = "") -> dict[str, Any]:
    if isinstance(item, str):
        return {"url": item, "method": "GET", "source": source or "text", "kind": "endpoint"}
    if not isinstance(item, dict):
        return {"text": str(item), "source": source, "kind": "text"}
    req = item.get("request") if isinstance(item.get("request"), dict) else {}
    return {
        "url": str(item.get("url") or req.get("url") or item.get("href") or item.get("endpoint") or ""),
        "method": str(item.get("method") or req.get("method") or "GET").upper(),
        "body": item.get("body") or item.get("postData") or req.get("body") or req.get("postData") or "",
        "text": " ".join(str(item.get(k) or "") for k in ("title", "label", "name", "text", "description")),
        "source": str(item.get("source") or source or item.get("type") or ""),
        "kind": str(item.get("kind") or item.get("type") or "endpoint"),
        "artifact_ref": str(item.get("artifact_ref") or item.get("artifact") or ""),
    }


def _filter_candidates(items: Any, value: str) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if value and value not in {str(item.get("name", "")).lower(), str(item.get("label", "")).lower()}:
            continue
        out.append(item)
    return out


def _candidate_tags(assets: list[BusinessCandidate], operations: list[BusinessCandidate], risks: list[BusinessCandidate]) -> list[str]:
    return [
        *(f"candidate_asset:{item.name}" for item in assets[:3]),
        *(f"candidate_operation:{item.name}" for item in operations[:3]),
        *(f"candidate_risk:{item.name}" for item in risks[:5]),
    ]


def _keyword_hits(haystack: str, keywords: list[str]) -> list[str]:
    return [kw for kw in keywords if kw.lower() in haystack]


def _haystack(evidence: BusinessEvidence) -> str:
    return " ".join([evidence.url, evidence.method, evidence.text_excerpt, " ".join(evidence.body_keys)]).lower()


def _source_bonus(source: str) -> float:
    src = source.lower()
    if any(token in src for token in ("proxy", "har", "browser_trace")):
        return 0.12
    if any(token in src for token in ("static_js", "js")):
        return 0.06
    return 0.0


def _body_keys(body: Any) -> list[str]:
    parsed = _try_json(body)
    if isinstance(parsed, dict):
        return sorted(str(k) for k in parsed.keys())[:40]
    return []


def _preview_body(body: Any) -> str:
    if body in (None, ""):
        return ""
    if isinstance(body, (dict, list)):
        return json.dumps(body, ensure_ascii=False, default=str)[:1000]
    return str(body)[:1000]


def _try_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except Exception:
            return None
    return None


def _excerpt(value: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _dedupe_candidates(items: list[BusinessCandidate]) -> list[BusinessCandidate]:
    seen: set[tuple[str, str]] = set()
    out = []
    for item in items:
        key = (item.category, item.name)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _load_surface(surface_json: str) -> dict[str, Any]:
    raw = _load_json(surface_json, {})
    if isinstance(raw, dict) and "surface" in raw and isinstance(raw["surface"], dict):
        return raw["surface"]
    return raw if isinstance(raw, dict) else {}


def _load_json(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value or "{}")
    except Exception:
        return fallback
