from argusai.core.workspace.cases import CaseStore


def test_case_graph_includes_evidence_artifact_and_finding_edges(tmp_path) -> None:
    store = CaseStore(tmp_path / "cases")
    case = store.create_case(title="Graph case")
    case_id = case["id"]

    artifact = store.add_artifact(
        case_id,
        kind="request",
        title="SQLi request",
        content="GET /login?id=1'",
        filename="request.txt",
    )
    evidence = store.add_evidence(
        case_id,
        kind="http",
        title="SQL error evidence",
        artifact_refs=[artifact["id"]],
        finding_uid="finding-sqli",
        summary="SQL error is reflected in response.",
    )
    link = store.add_link(
        case_id,
        source_ref="todo:validate.login-sqli",
        relation="produced",
        target_ref=f"evidence:{evidence['id']}",
    )

    graph = store.graph(case_id)

    node_ids = {node["id"] for node in graph["nodes"]}
    edge_relations = {(edge["source_ref"], edge["relation"], edge["target_ref"]) for edge in graph["edges"]}

    assert f"artifact:{artifact['id']}" in node_ids
    assert f"evidence:{evidence['id']}" in node_ids
    assert "finding:finding-sqli" in node_ids
    assert ("todo:validate.login-sqli", "produced", f"evidence:{evidence['id']}") in edge_relations
    assert (f"evidence:{evidence['id']}", "supported_by", f"artifact:{artifact['id']}") in edge_relations
    assert ("finding:finding-sqli", "validated_by", f"evidence:{evidence['id']}") in edge_relations
    assert link["relation"] == "produced"

