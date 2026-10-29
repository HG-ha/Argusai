from __future__ import annotations

from argusai.core.workspace.cases import (
    ARTIFACT_KINDS,
    CASE_TARGET_TYPES,
    EVIDENCE_KINDS,
    ArtifactRef,
    CaseTarget,
    EvidenceRef,
)
from argusai.core.assistant.skills import EXPLICIT_SCHEMAS


def test_cross_domain_case_kinds_are_preserved() -> None:
    assert CaseTarget(type="cluster", value="dev").to_dict()["type"] == "cluster"
    assert ArtifactRef(id="art-1", kind="sbom", path="artifacts/a.json").to_dict()["kind"] == "sbom"
    assert EvidenceRef(id="ev-1", kind="ai").to_dict()["kind"] == "ai"
    assert EvidenceRef(id="ev-2", kind="hardware").to_dict()["kind"] == "hardware"


def test_unknown_case_kinds_still_fail_closed_to_other() -> None:
    assert CaseTarget(type="unknown-new-domain", value="x").to_dict()["type"] == "other"
    assert ArtifactRef(id="art-1", kind="unknown-kind", path="a").to_dict()["kind"] == "other"
    assert EvidenceRef(id="ev-1", kind="unknown-kind").to_dict()["kind"] == "other"


def test_expected_domain_kind_catalogue_is_available() -> None:
    assert {"cluster", "ad_domain", "firmware", "board"} <= CASE_TARGET_TYPES
    assert {"ai_prompt", "tool_call_log", "manifest", "graph", "uart"} <= ARTIFACT_KINDS
    assert {"ai", "dom", "tool_call", "cloud", "directory", "sca", "wireless"} <= EVIDENCE_KINDS


def test_case_tool_schemas_describe_domain_kinds_without_hard_enum() -> None:
    evidence_kind = EXPLICIT_SCHEMAS["evidence_add"]["kind"]
    artifact_kind = EXPLICIT_SCHEMAS["artifact_add"]["kind"]
    assert "ai" in evidence_kind["description"]
    assert "hardware" in evidence_kind["description"]
    assert "sbom" in artifact_kind["description"]
    assert "enum" not in evidence_kind
    assert "enum" not in artifact_kind
